package session

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

func readProc(pid int) (Proc, int, bool) {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return Proc{}, 0, false
	}
	stat := string(raw)
	// The name sits in parentheses and may itself contain spaces or parentheses.
	open, close := strings.IndexByte(stat, '('), strings.LastIndexByte(stat, ')')
	if open < 0 || close < open || close+2 > len(stat) {
		return Proc{}, 0, false
	}
	rest := strings.Fields(stat[close+2:])
	if len(rest) < 20 {
		return Proc{}, 0, false
	}
	parent, err1 := strconv.Atoi(rest[1])
	started, err2 := strconv.ParseInt(rest[19], 10, 64)
	if err1 != nil || err2 != nil {
		return Proc{}, 0, false
	}
	return Proc{PID: pid, Name: normalName(stat[open+1 : close]), Started: started}, parent, true
}
