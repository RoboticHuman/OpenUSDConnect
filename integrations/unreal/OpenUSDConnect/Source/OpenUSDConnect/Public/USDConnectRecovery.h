// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "CoreMinimal.h"
#include "USDConnectRecovery.generated.h"

UENUM(BlueprintType)
enum class EUSDConnectRecoveryDisposition : uint8
{
	None UMETA(DisplayName="None"),
	SessionFatal UMETA(DisplayName="Session Fatal"),
	RecoverableConflict UMETA(DisplayName="Recoverable Conflict"),
	InvalidOperation UMETA(DisplayName="Invalid Operation"),
};
