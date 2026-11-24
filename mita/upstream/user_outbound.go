// SPDX-License-Identifier: GPL-3.0-or-later

package socks5

import (
	"github.com/enfein/mieru/v3/pkg/appctl/appctlpb"
	"github.com/enfein/mieru/v3/pkg/egress"
)

// SetProxyOutbounds replaces the immutable snapshot used by new requests.
func (s *Server) SetProxyOutbounds(users []*appctlpb.User) {
	outbounds := make(map[string]string)
	for _, user := range users {
		if name := user.GetProxyOutbound(); name != "" {
			outbounds[user.GetName()] = name
		}
	}
	s.proxyOutbounds.Store(&outbounds)
}

func (s *Server) selectUserOutbound(user string, action egress.Action) egress.Action {
	if action.Action != appctlpb.EgressAction_PROXY {
		return action
	}
	outbounds := s.proxyOutbounds.Load()
	if outbounds == nil {
		return action
	}
	name := (*outbounds)[user]
	if name == "" {
		return action
	}
	for _, proxy := range s.config.Egress.GetProxies() {
		if proxy.GetName() == name {
			return egress.Action{Action: appctlpb.EgressAction_PROXY, Proxy: proxy}
		}
	}
	// A configured override must never fall back to the rule's proxy.
	return egress.Action{Action: appctlpb.EgressAction_REJECT}
}
