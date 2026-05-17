package manager

import (
	"context"
	"fmt"
	"os/exec"
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
	// Stage 2: FFmpeg decode smoke test (first 60 seconds)
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

	// Primary probe with standard settings
	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-show_entries",
		"format=duration:stream=codec_type,duration", "-of", "default=noprint_wrappers=1:nokey=1:pairs_sep=\\n",
		filepath)

	if out, err := cmd.CombinedOutput(); err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("probe timeout")
		}
		// If output is empty or minimal, try fallback probe with higher analyzeduration
		if len(out) < 10 {
			v.logger.Debug().Str("file", filepath).Msg("Fallback ffprobe with higher analyzeduration")
			cmd = exec.CommandContext(ctx, "ffprobe", "-v", "error",
				"-analyzeduration", "100M", "-probesize", "100M",
				"-show_entries", "format=duration:stream=codec_type,duration",
				"-of", "default=noprint_wrappers=1:nokey=1:pairs_sep=\\n", filepath)
			if _, err := cmd.CombinedOutput(); err != nil {
				return fmt.Errorf("fallback probe also failed: %w", err)
			}
		}
	}

	return nil
}

// runFFmpegDecode runs a quick decode test on video/audio streams (max 60 seconds)
func (v *FileValidator) runFFmpegDecode(ctx context.Context, filepath string) error {
	ctx, cancel := context.WithTimeout(ctx, v.DecodeTimeout)
	defer cancel()

	// Decode first 60 seconds of primary video and audio streams only
	cmd := exec.CommandContext(ctx, "ffmpeg",
		"-v", "error", "-xerror", "-t", "60",
		"-i", filepath,
		"-map", "0:v:0", "-map", "0:a:0",
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
	}

	return nil
}
