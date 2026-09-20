#pragma once

#include <cstdio>
#include <cstdlib>

// Evaluate checks exactly once in every build configuration.
#define CHECK(expression)                                                                          \
	do                                                                                             \
	{                                                                                              \
		if (!(expression))                                                                         \
		{                                                                                          \
			std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #expression);    \
			std::exit(EXIT_FAILURE);                                                               \
		}                                                                                          \
	} while (false)
