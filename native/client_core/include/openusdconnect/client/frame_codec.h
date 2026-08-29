#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace openusdconnect::client
{

inline constexpr std::size_t kFrameHeaderSize = 4;
inline constexpr std::size_t kDefaultMaxFrameSize = 16 * 1024 * 1024;

enum class FrameResult : std::uint8_t
{
	Success,
	InvalidMaxFrameSize,
	EmptyPayload,
	PayloadTooLarge,
	InvalidHeader,
};

[[nodiscard]] bool IsValidMaxFrameSize(std::size_t max_frame_size) noexcept;

class FrameDecoder final
{
public:
	explicit FrameDecoder(std::size_t max_frame_size = kDefaultMaxFrameSize);

	[[nodiscard]] FrameResult Feed(const std::uint8_t* data, std::size_t size,
								   std::vector<std::vector<std::uint8_t>>& frames);
	void Reset() noexcept;

	[[nodiscard]] std::size_t BufferedBytes() const noexcept;
	[[nodiscard]] std::size_t MaxFrameSize() const noexcept;

private:
	void Compact();

	std::vector<std::uint8_t> Buffer;
	std::size_t Cursor = 0;
	std::size_t ExpectedPayloadSize = 0;
	std::size_t MaxFrameSizeValue;
};

[[nodiscard]] bool TryReadFrameHeader(const std::uint8_t* header, std::size_t max_frame_size,
									  std::size_t& payload_size) noexcept;

[[nodiscard]] FrameResult EncodeFrame(const std::uint8_t* payload, std::size_t size,
									  std::vector<std::uint8_t>& frame,
									  std::size_t max_frame_size = kDefaultMaxFrameSize);

[[nodiscard]] FrameResult
WriteFrameHeader(std::size_t payload_size, std::uint8_t* destination,
				 std::size_t max_frame_size = kDefaultMaxFrameSize) noexcept;

[[nodiscard]] FrameResult
EncodeFrameInto(const std::uint8_t* payload, std::size_t size, std::uint8_t* destination,
				std::size_t max_frame_size = kDefaultMaxFrameSize) noexcept;

} // namespace openusdconnect::client
