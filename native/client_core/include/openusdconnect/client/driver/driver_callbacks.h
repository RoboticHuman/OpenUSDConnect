#pragma once

#include "openusdconnect/client/engine/actions.h"
#include "openusdconnect/client/engine/notification.h"

#include <functional>
#include <optional>
#include <string>

namespace openusdconnect::client
{

// Every callback is optional, must return normally, and runs on the driver
// thread, which holds no driver or endpoint lock while calling it.
struct DriverCallbacks final
{
	// Read just before each handshake; nullopt abandons that connection attempt.
	std::function<std::optional<std::string>()> Token;
	// When set, the driver drains the notification queue into it after every
	// endpoint call, so a notification is handled before the next attempt.
	std::function<void(Notification)> Notifications;
	std::function<void(LogLevel, const std::string&)> Log;
	// The driver thread's last action; it may destroy the driver.
	std::function<void()> Exited;
};

} // namespace openusdconnect::client
