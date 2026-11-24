// SPDX-License-Identifier: GPL-3.0-or-later

package socks5

import (
	"context"
	"testing"

	pb "github.com/enfein/mieru/v3/pkg/appctl/appctlpb"
	"google.golang.org/protobuf/proto"
)

func TestPerUserProxyOutbound(t *testing.T) {
	server := &Server{config: &Config{Egress: &pb.Egress{
		Proxies: []*pb.EgressProxy{
			{Name: proto.String("default")},
			{Name: proto.String("west")},
			{Name: proto.String("east")},
		},
		Rules: []*pb.EgressRule{
			{DomainNames: []string{"blocked.example"}, Action: pb.EgressAction_REJECT.Enum()},
			{DomainNames: []string{"direct.example"}, Action: pb.EgressAction_DIRECT.Enum()},
			{DomainNames: []string{"proxy.example"}, ProxyNames: []string{"default"}, Action: pb.EgressAction_PROXY.Enum()},
		},
	}}}
	user := func(name, outbound string) *pb.User {
		return &pb.User{Name: proto.String(name), ProxyOutbound: proto.String(outbound)}
	}
	server.SetProxyOutbounds([]*pb.User{
		user("alice", "west"), user("bob", "east"), user("stale", "missing"),
	})
	check := func(username, domain string, action pb.EgressAction, proxy string) {
		t.Helper()
		got := server.FindAction(context.Background(), makeSocks5ConnectDomainInput(domain, username))
		if got.Action != action || got.Proxy.GetName() != proxy {
			t.Errorf("%s to %s: got %s/%q, want %s/%q", username, domain, got.Action, got.Proxy.GetName(), action, proxy)
		}
	}
	check("alice", "proxy.example", pb.EgressAction_PROXY, "west")
	check("bob", "proxy.example", pb.EgressAction_PROXY, "east")
	check("default-user", "proxy.example", pb.EgressAction_PROXY, "default")
	check("stale", "proxy.example", pb.EgressAction_REJECT, "")
	check("alice", "blocked.example", pb.EgressAction_REJECT, "")
	check("alice", "direct.example", pb.EgressAction_DIRECT, "")
	check("alice", "unmatched.example", pb.EgressAction_DIRECT, "")
	server.SetProxyOutbounds([]*pb.User{user("alice", "east")})
	check("alice", "proxy.example", pb.EgressAction_PROXY, "east")
	check("bob", "proxy.example", pb.EgressAction_PROXY, "default")
}
