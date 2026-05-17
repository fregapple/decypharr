package manager

import (
	"context"
	"fmt"
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
	ProbeTimeout  time.Duration
	DecodeTimeout time.Duration
}

// NewFileValidator creates a validator with hardcoded settings
func NewFileValidator() *FileValidator {
	return &FileValidator{
		logger:        logger.New("validator"),
		ProbeTimeout:  30 * time.Second,
		DecodeTimeout: 60 * time.Second,
	}
}

// ValidateFile runs ffprobe and ffmpeg checks on a file.
// Returns broken=true if validation fails, along with a reason string.
func (v *FileValidator) ValidateFile(ctx context.Context, filepath string) (broken bool, reason string) {
	v.logger.Debug().Str("file", filepath).Msg("ValidateFile: starting ffprobe stage")
	// Stage 1: FFprobe with metadata check
	if err := v.runFFprobe(ctx, filepath); err != nil {
		v.logger.Info().Err(err).Str("file", filepath).Msg("ValidateFile: ffprobe failed")
		return true, fmt.Sprintf("ffprobe failed: %v", err)
	}

	v.logger.Debug().Str("file", filepath).Msg("ValidateFile: ffprobe passed, starting ffmpeg decode stage")
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

	// Primary probe with JSON output, matching the manual repro more closely.
	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-probesize", "50000000", filepath)

	if out, err := cmd.CombinedOutput(); err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("probe timeout")
		}
		// If output is empty or minimal, try fallback probe with higher analyzeduration
		if len(out) < 10 {
			v.logger.Debug().Str("file", filepath).Msg("Fallback ffprobe with higher analyzeduration")
			cmd = exec.CommandContext(ctx, "ffprobe", "-v", "error",
				"-analyzeduration", "100M", "-probesize", "100M",
				"-print_format", "json", "-show_format", "-show_streams", filepath)
			if out, err := cmd.CombinedOutput(); err != nil {
				return fmt.Errorf("fallback probe also failed: %w", err)
			} else if hasDecoderErrors(out) {
				return fmt.Errorf("fallback probe reported decoder errors")
			}
		} else if hasDecoderErrors(out) {
			return fmt.Errorf("probe reported decoder errors: %s", strings.TrimSpace(string(out)))
		}
	} else if hasDecoderErrors(out) {
		return fmt.Errorf("probe reported decoder errors: %s", strings.TrimSpace(string(out)))
	}

	return nil
}

// runFFmpegDecode runs a full decode test on all mapped streams.
func (v *FileValidator) runFFmpegDecode(ctx context.Context, filepath string) error {
	ctx, cancel := context.WithTimeout(ctx, v.DecodeTimeout)
	defer cancel()

	// Decode the full file and all streams so corruption in later segments or
	// secondary tracks is surfaced.
	cmd := exec.CommandContext(ctx, "ffmpeg",
		"-v", "error", "-xerror",
		"-i", filepath,
		"-map", "0",
		"-f", "null", "-")

	if out, err := cmd.CombinedOutput(); err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("decode timeout")
		}
		// Check if error is about missing streams (acceptable for audio-only, etc)
		// Only fail on actual decode errors (NAL unit, frame errors, etc)
		if len(out) > 0 {
			return fmt.Errorf("decode error: %s", string(out))
		}
		// -map errors are OK if streams don't exist; actual corruption shows explicit errors
		return nil
	} else if hasDecoderErrors(out) {
		return fmt.Errorf("decode reported decoder errors: %s", strings.TrimSpace(string(out)))
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
