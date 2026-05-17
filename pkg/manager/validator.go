package manager

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/rs/zerolog"

	"github.com/sirrobot01/decypharr/internal/logger"
)

// FileValidator provides ffprobe/ffmpeg validation for media files
type FileValidator struct {
	logger zerolog.Logger

	// Hardcoded configuration
	ProbeTimeout    time.Duration
	DecodeTimeout   time.Duration
	DecodeWindowSec int
	DeepScan        bool
}

// NewFileValidator creates a validator with hardcoded settings
func NewFileValidator() *FileValidator {
	deep := strings.EqualFold(strings.TrimSpace(os.Getenv("DECYPHARR_VALIDATOR_DEEP_SCAN")), "true") || strings.TrimSpace(os.Getenv("DECYPHARR_VALIDATOR_DEEP_SCAN")) == "1"
	decodeTimeout := 60 * time.Second
	decodeWindowSec := 60
	if deep {
		// Full-file decode can be very long on large media.
		decodeTimeout = 30 * time.Minute
		decodeWindowSec = 0
	}

	return &FileValidator{
		logger:          logger.New("validator"),
		ProbeTimeout:    30 * time.Second,
		DecodeTimeout:   decodeTimeout,
		DecodeWindowSec: decodeWindowSec,
		DeepScan:        deep,
	}
}

// ValidateFile runs ffprobe and ffmpeg checks on a file.
// Returns broken=true if validation fails, along with a reason string.
func (v *FileValidator) ValidateFile(ctx context.Context, filepath string) (broken bool, reason string) {
	// Quick existence/size check before shelling out to ffprobe.
	fi, statErr := os.Stat(filepath)
	if statErr != nil {
		v.logger.Warn().Err(statErr).Str("file", filepath).Msg("ValidateFile: file not accessible, skipping")
		return false, ""
	}
	v.logger.Debug().Str("file", filepath).Int64("size_bytes", fi.Size()).Msg("ValidateFile: starting ffprobe stage")
	// Stage 1: FFprobe with metadata check
	if err := v.runFFprobe(ctx, filepath); err != nil {
		v.logger.Info().Err(err).Str("file", filepath).Msg("ValidateFile: ffprobe failed")
		return true, fmt.Sprintf("ffprobe failed: %v", err)
	}

	v.logger.Debug().
		Str("file", filepath).
		Bool("deep_scan", v.DeepScan).
		Int("decode_window_sec", v.DecodeWindowSec).
		Msg("ValidateFile: ffprobe passed, starting ffmpeg decode stage")
	// Stage 2: FFmpeg decode validation
	if err := v.runFFmpegDecode(ctx, filepath); err != nil {
		v.logger.Info().Err(err).Str("file", filepath).Msg("ValidateFile: ffmpeg decode failed")
		return true, fmt.Sprintf("ffmpeg decode failed: %v", err)
	}

	v.logger.Debug().Str("file", filepath).Msg("ValidateFile: passed all checks")
	return false, ""
}

// runFFprobe executes ffprobe to validate metadata
func (v *FileValidator) runFFprobe(ctx context.Context, filepath string) error {
	ctx, cancel := context.WithTimeout(ctx, v.ProbeTimeout)
	defer cancel()

	// Primary probe — collect combined output regardless of exit code so we
	// can inspect stderr decoder warnings on files that exit 0.
	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-probesize", "50000000", filepath)
	out, err := cmd.CombinedOutput()

	// Always log a truncated snippet at Trace so we can see what ffprobe produced.
	snippet := strings.TrimSpace(string(out))
	if len(snippet) > 500 {
		snippet = snippet[:500] + "..."
	}
	v.logger.Trace().Str("file", filepath).Str("ffprobe_output", snippet).Err(err).Msg("ValidateFile: raw ffprobe output")

	if hasDecoderErrors(out) {
		return fmt.Errorf("probe reported decoder errors: %s", strings.TrimSpace(string(out)))
	}

	if err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("probe timeout")
		}
		// Very little output usually means the demuxer couldn't open the file at all;
		// try again with higher buffer settings.
		if len(out) < 10 {
			v.logger.Debug().Str("file", filepath).Msg("Fallback ffprobe with higher analyzeduration")
			cmd2 := exec.CommandContext(ctx, "ffprobe", "-v", "error",
				"-analyzeduration", "100M", "-probesize", "100M",
				"-print_format", "json", "-show_format", "-show_streams", filepath)
			out2, err2 := cmd2.CombinedOutput()
			if err2 != nil {
				return fmt.Errorf("fallback probe also failed: %w", err2)
			}
			if hasDecoderErrors(out2) {
				return fmt.Errorf("fallback probe reported decoder errors")
			}
		} else {
			return fmt.Errorf("probe failed: %w", err)
		}
	}

	return nil
}

// runFFmpegDecode runs either a fast 60s smoke test (default) or full-file
// decode (deep-scan mode).
func (v *FileValidator) runFFmpegDecode(ctx context.Context, filepath string) error {
	ctx, cancel := context.WithTimeout(ctx, v.DecodeTimeout)
	defer cancel()

	args := []string{"-v", "error", "-xerror"}
	if !v.DeepScan && v.DecodeWindowSec > 0 {
		args = append(args, "-t", fmt.Sprintf("%d", v.DecodeWindowSec))
	}
	args = append(args, "-i", filepath)
	if v.DeepScan {
		args = append(args, "-map", "0")
	} else {
		args = append(args, "-map", "0:v:0", "-map", "0:a:0")
	}
	args = append(args, "-f", "null", "-")

	v.logger.Debug().Str("file", filepath).Strs("args", args).Msg("ValidateFile: running ffmpeg")

	start := time.Now()
	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	out, err := cmd.CombinedOutput()
	elapsed := time.Since(start)

	snippet := strings.TrimSpace(string(out))
	if len(snippet) > 500 {
		snippet = snippet[:500] + "..."
	}
	v.logger.Debug().
		Str("file", filepath).
		Dur("elapsed", elapsed).
		Int("exit_code", cmd.ProcessState.ExitCode()).
		Int("output_bytes", len(out)).
		Str("ffmpeg_output", snippet).
		Msg("ValidateFile: raw ffmpeg output")

	if err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("decode timeout after %s", elapsed)
		}
		// Non-zero exit: if there is any output, treat it as a decode error.
		// Empty output with non-zero exit (e.g. stream-map mismatch) is not corruption.
		if len(strings.TrimSpace(string(out))) > 0 {
			return fmt.Errorf("decode error: %s", strings.TrimSpace(string(out)))
		}
		return nil
	}

	// Exit 0 but stderr had decoder warnings (e.g. EAC3 bitstream errors that
	// ffmpeg recovers from but still indicate a broken file).
	if hasDecoderErrors(out) {
		return fmt.Errorf("decode reported decoder errors: %s", snippet)
	}

	return nil
}

func hasDecoderErrors(out []byte) bool {
	text := strings.ToLower(string(out))
	patterns := []string{
		"error decoding",
		"invalid bitstream",
		"unable to determine channel mode",
		"new coupling strategy must be present in block 0",
		"exponent -1 is out-of-range",
		"invalid data found when processing input",
		"error while decoding",
	}
	for _, pattern := range patterns {
		if strings.Contains(text, pattern) {
			return true
		}
	}
	return false
}
