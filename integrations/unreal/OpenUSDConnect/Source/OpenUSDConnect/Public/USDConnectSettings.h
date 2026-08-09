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
	 * Layer/department name sent by emitter-only sessions.
	 * The native Unreal receiver does not yet implement managed layered replay,
	 * so bidirectional/receive sessions fail closed when this is non-empty.
	 */
	UPROPERTY(config, EditAnywhere, Category="Server", meta=(DisplayName="Department"))
	FString Department = TEXT("");

	/** Automatically connect when the game world starts */
	UPROPERTY(config, EditAnywhere, Category="Connection", meta=(DisplayName="Auto Connect on World Start"))
	bool bAutoConnect = true;

	/**
	 * When the opened USD root layer contains customLayerData["openusdconnect"],
	 * use that metadata to configure the live sync endpoint.
	 */
	UPROPERTY(config, EditAnywhere, Category="Live Open", meta=(DisplayName="Use USD Live Metadata"))
	bool bUseLiveMetadataFromStage = true;

	/** Auto-start the receiver when live metadata is detected on the opened stage. */
	UPROPERTY(config, EditAnywhere, Category="Live Open", meta=(DisplayName="Auto-start Receiver from Metadata"))
	bool bAutoStartReceiverFromLiveMetadata = true;

	/** Auto-start the emitter when live metadata is detected on the opened stage. */
	UPROPERTY(config, EditAnywhere, Category="Live Open", meta=(DisplayName="Auto-start Emitter from Metadata"))
	bool bAutoStartEmitterFromLiveMetadata = true;

	/** Persist TOFU auth tokens in the user's Unreal config and reuse them on reconnect. */
	UPROPERTY(config, EditAnywhere, Category="Authentication", meta=(DisplayName="Persist Auth Tokens"))
	bool bPersistAuthTokens = true;

	/** Seconds between reconnection attempts after a disconnect */
	UPROPERTY(config, EditAnywhere, Category="Connection", meta=(DisplayName="Reconnect Delay (s)", ClampMin=1, ClampMax=60))
	float ReconnectDelaySecs = 3.0f;

	// UDeveloperSettings interface
	virtual FName GetCategoryName() const override { return TEXT("Plugins"); }
};
