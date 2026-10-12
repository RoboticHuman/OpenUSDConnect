#pragma once

#include "openusdconnect/client/engine/receiver_endpoint.h"

#include "frames.h"
#include "test_check.h"

#include <cstdint>
#include <vector>

// Receiver test fixtures shared by the endpoint and driver tests.
namespace receiver_test
{

using namespace endpoint_test;

[[nodiscard]] inline ReceiverConfig TestConfig()
{
	ReceiverConfig config;
	config.Host = "127.0.0.1";
	config.Port = 7200;
	config.ClientId = "client";
	config.Origin = "origin";
	return config;
}

[[nodiscard]] inline std::vector<std::int32_t> Sequences(const std::vector<Bytes>& frames)
{
	std::vector<std::int32_t> sequences;
	for (const Bytes& frame : frames)
	{
		if (const auto* event = Decode(frame).payload_as_BroadcastEvent())
		{
			sequences.push_back(event->seq());
		}
	}
	return sequences;
}

} // namespace receiver_test
