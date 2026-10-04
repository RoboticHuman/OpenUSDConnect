#include "openusdconnect/client/driver/socket.h"

#include <system_error>

namespace openusdconnect::client
{

std::string DescribeSystemError(int system_error)
{
	return std::system_category().message(system_error);
}

std::string Describe(const TransportFailure& failure)
{
	return failure.Result == SocketResult::Timeout ? std::string("timed out")
												   : DescribeSystemError(failure.SystemError);
}

} // namespace openusdconnect::client
