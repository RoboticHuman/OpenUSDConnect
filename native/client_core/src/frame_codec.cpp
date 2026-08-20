#include "openusdconnect/client/frame_codec.h"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <limits>

namespace openusdconnect::client
{
namespace
{

[[nodiscard]] std::uint32_t ReadBigEndianU32(const std::uint8_t* bytes) noexcept
{
	return (static_cast<std::uint32_t>(bytes[0]) << 24U) |
		   (static_cast<std::uint32_t>(bytes[1]) << 16U) |
		   (static_cast<std::uint32_t>(bytes[2]) << 8U) | static_cast<std::uint32_t>(bytes[3]);
}

void WriteBigEndianU32(std::uint8_t* bytes, std::uint32_t value) noexcept
{
	bytes[0] = static_cast<std::uint8_t>(value >> 24U);
	bytes[1] = static_cast<std::uint8_t>(value >> 16U);
	bytes[2] = static_cast<std::uint8_t>(value >> 8U);
	bytes[3] = static_cast<std::uint8_t>(value);
}

[[nodiscard]] FrameResult ValidateFrameSize(std::size_t size, std::size_t max_frame_size) noexcept
{
	if (!IsValidMaxFrameSize(max_frame_size))
	{
		return FrameResult::InvalidMaxFrameSize;
	}
	if (size == 0)
	{
		return FrameResult::EmptyPayload;
	}
	if (size > max_frame_size)
	{
		return FrameResult::PayloadTooLarge;
	}
	return FrameResult::Success;
}

} // namespace

bool IsValidMaxFrameSize(std::size_t max_frame_size) noexcept
{
	return max_frame_size != 0 && max_frame_size <= std::numeric_limits<std::uint32_t>::max();
}

FrameDecoder::FrameDecoder(std::size_t max_frame_size)
	: MaxFrameSizeValue(max_frame_size)
{
	assert(IsValidMaxFrameSize(MaxFrameSizeValue));
	Buffer.reserve(std::min<std::size_t>(MaxFrameSizeValue + kFrameHeaderSize, 64 * 1024));
}

FrameResult FrameDecoder::Feed(const std::uint8_t* data, std::size_t size,
							   std::vector<std::vector<std::uint8_t>>& frames)
{
	assert(data != nullptr || size == 0);
	frames.clear();
	if (size != 0)
	{
		Buffer.insert(Buffer.end(), data, data + size);
	}

	while (true)
	{
		const std::size_t available = Buffer.size() - Cursor;
		if (ExpectedPayloadSize == 0)
		{
			if (available < kFrameHeaderSize)
			{
				break;
			}
			if (!TryReadFrameHeader(Buffer.data() + Cursor, MaxFrameSizeValue, ExpectedPayloadSize))
			{
				Reset();
				frames.clear();
				return FrameResult::InvalidHeader;
			}
			Cursor += kFrameHeaderSize;
		}

		if (Buffer.size() - Cursor < ExpectedPayloadSize)
		{
			break;
		}

		const auto begin = Buffer.begin() + static_cast<std::ptrdiff_t>(Cursor);
		frames.emplace_back(begin, begin + static_cast<std::ptrdiff_t>(ExpectedPayloadSize));
		Cursor += ExpectedPayloadSize;
		ExpectedPayloadSize = 0;
	}

	Compact();
	return FrameResult::Success;
}

void FrameDecoder::Reset() noexcept
{
	Buffer.clear();
	Cursor = 0;
	ExpectedPayloadSize = 0;
}

std::size_t FrameDecoder::BufferedBytes() const noexcept
{
	return Buffer.size() - Cursor;
}

std::size_t FrameDecoder::MaxFrameSize() const noexcept
{
	return MaxFrameSizeValue;
}

void FrameDecoder::Compact()
{
	if (Cursor == Buffer.size())
	{
		Buffer.clear();
		Cursor = 0;
		return;
	}
	if (Cursor < 64 * 1024 || Cursor * 2 < Buffer.size())
	{
		return;
	}
	const std::size_t remaining = Buffer.size() - Cursor;
	std::memmove(Buffer.data(), Buffer.data() + Cursor, remaining);
	Buffer.resize(remaining);
	Cursor = 0;
}

bool TryReadFrameHeader(const std::uint8_t* header, std::size_t max_frame_size,
						std::size_t& payload_size) noexcept
{
	payload_size = ReadBigEndianU32(header);
	return payload_size != 0 && payload_size <= max_frame_size;
}

FrameResult EncodeFrame(const std::uint8_t* payload, std::size_t size,
						std::vector<std::uint8_t>& frame, std::size_t max_frame_size)
{
	assert(payload != nullptr || size == 0);
	const FrameResult result = ValidateFrameSize(size, max_frame_size);
	if (result != FrameResult::Success)
	{
		frame.clear();
		return result;
	}
	frame.resize(kFrameHeaderSize + size);
	WriteBigEndianU32(frame.data(), static_cast<std::uint32_t>(size));
	std::memcpy(frame.data() + kFrameHeaderSize, payload, size);
	return FrameResult::Success;
}

FrameResult WriteFrameHeader(std::size_t payload_size, std::uint8_t* destination,
							 std::size_t max_frame_size) noexcept
{
	assert(destination != nullptr);
	const FrameResult result = ValidateFrameSize(payload_size, max_frame_size);
	if (result != FrameResult::Success)
	{
		return result;
	}
	WriteBigEndianU32(destination, static_cast<std::uint32_t>(payload_size));
	return FrameResult::Success;
}

FrameResult EncodeFrameInto(const std::uint8_t* payload, std::size_t size,
							std::uint8_t* destination, std::size_t max_frame_size) noexcept
{
	assert(payload != nullptr || size == 0);
	assert(destination != nullptr);
	const FrameResult result = WriteFrameHeader(size, destination, max_frame_size);
	if (result != FrameResult::Success)
	{
		return result;
	}
	std::memcpy(destination + kFrameHeaderSize, payload, size);
	return FrameResult::Success;
}

} // namespace openusdconnect::client
