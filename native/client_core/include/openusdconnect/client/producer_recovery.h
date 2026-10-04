#pragma once

#include <cstdint>

namespace openusdconnect::client
{

enum class ProducerRecoveryDisposition : std::uint8_t
{
	None,
	RecoverableConflict,
	InvalidOperation,
	SessionFatal,
};

} // namespace openusdconnect::client
