package internalapi

import (
	"errors"
	"fmt"
	"net"
	"strings"
)

// AllInterfacesOverrideEnv is the environment variable that lets the
// internal listener bind every interface anyway. Setting it to "true" is
// an explicit act, and main.go says so at WARN every time it starts.
const AllInterfacesOverrideEnv = "INTERNAL_ADDR_ALLOW_ALL_INTERFACES"

// CheckListenAddr judges an INTERNAL_ADDR before anything binds it.
//
// It returns an error for an address that is not `host:port`, and
// allInterfaces=true for a host that means "every interface": an empty
// host (`:8081`), `0.0.0.0`, `::` and their spellings, and the legacy
// inet_aton shorthands (`0`, `0.0`) that some resolvers read as 0.0.0.0.
//
// WHY. The route this listener serves is authenticated by the caller's
// network position plus a lease owner. On a PaaS that publishes whatever a
// process listens on, `:8081` is a public token route (PR #52's review,
// L3). The default is loopback; compose binds the service name or opts in
// through AllInterfacesOverrideEnv, and both are documented in
// docs/internal-api.md.
func CheckListenAddr(addr string) (allInterfaces bool, err error) {
	host, port, err := net.SplitHostPort(strings.TrimSpace(addr))
	if err != nil {
		return false, fmt.Errorf("INTERNAL_ADDR must be host:port: %w", err)
	}
	if port == "" {
		return false, errors.New("INTERNAL_ADDR must name a port")
	}
	if host == "" {
		return true, nil
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsUnspecified(), nil
	}
	// Not a full IP literal. Digits and dots only is the inet_aton
	// shorthand family ("0", "0.0", "127.1"); Go's own resolver refuses
	// it and libc's may read it as a wildcard, so it is refused as one.
	if strings.Trim(host, "0123456789.") == "" {
		return true, nil
	}
	return false, nil
}
