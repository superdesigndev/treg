package session

import (
	"golang.org/x/sys/unix"
)

func readProc(pid int) (Proc, int, bool) {
	info, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	if err != nil || int(info.Proc.P_pid) != pid {
		return Proc{}, 0, false
	}
	name := unix.ByteSliceToString(info.Proc.P_comm[:])
	return Proc{PID: pid, Name: normalName(name), Started: info.Proc.P_starttime.Sec}, int(info.Eproc.Ppid), true
}
