#pragma once

#include "openusdconnect/client/driver/socket.h"

#include <memory>

namespace openusdconnect::client
{

// Winsock or BSD sockets with TCP_NODELAY, so small control frames go out at once.
class TcpSocketFactory final : public SocketFactory
{
public:
	TcpSocketFactory();

	[[nodiscard]] std::unique_ptr<Socket> Create() override;
};

} // namespace openusdconnect::client
