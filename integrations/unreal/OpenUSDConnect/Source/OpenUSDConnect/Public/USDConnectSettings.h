// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "Engine/DeveloperSettings.h"
#include "USDConnectSettings.generated.h"

/**
 * Project-wide settings for the OpenUSD Connect plugin.
 * Edit via: Edit > Project Settings > OpenUSD Connect
 */
UCLASS(config=Game, defaultconfig, meta=(DisplayName="OpenUSD Connect"))
class OPENUSDCONNECT_API UUSDConnectSettings : public UDeveloperSettings
{
	GENERATED_BODY()

public:
	UUSDConnectSettings();

	/** Hostname or IP of the OpenUSDConnect server */
	UPROPERTY(config, EditAnywhere, Category="Server", meta=(DisplayName="Server Host"))
	FString ServerHost = TEXT("127.0.0.1");

	/** TCP port of the OpenUSDConnect server */
	UPROPERTY(config, EditAnywhere, Category="Server", meta=(DisplayName="Server Port", ClampMin=1, ClampMax=65535))
	int32 ServerPort = 7200;

	/**
	 * Layer/department name sent in the HELLO message.
	 * Leave empty to use the default (no per-department layer separation).
	 */
	UPROPERTY(config, EditAnywhere, Category="Server", meta=(DisplayName="Department"))
	FString Department = TEXT("");

	/** Automatically connect when the game world starts */
	UPROPERTY(config, EditAnywhere, Category="Connection", meta=(DisplayName="Auto Connect on World Start"))
	bool bAutoConnect = true;

	/** Seconds between reconnection attempts after a disconnect */
	UPROPERTY(config, EditAnywhere, Category="Connection", meta=(DisplayName="Reconnect Delay (s)", ClampMin=1, ClampMax=60))
	float ReconnectDelaySecs = 3.0f;

	// UDeveloperSettings interface
	virtual FName GetCategoryName() const override { return TEXT("Plugins"); }
};
