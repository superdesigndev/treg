//go:build !linux && !darwin

package session

func readProc(int) (Proc, int, bool) { return Proc{}, 0, false }
