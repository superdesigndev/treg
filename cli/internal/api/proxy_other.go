//go:build !darwin && !windows

package api

// Elsewhere urllib has no system settings beyond the environment.
func readSystemProxy() systemSettings { return systemSettings{} }
