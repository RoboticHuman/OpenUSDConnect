#pragma once

#include <chrono>

namespace openusdconnect::client
{

// Hosts pass the current time in; the engine never reads a clock.
using TimePoint = std::chrono::steady_clock::time_point;

} // namespace openusdconnect::client
