// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#include "USDConnectSubsystem.h"

#include "EndpointRunner.h"
#include "USDConnectSettings.h"
#include "USDEventApplier.h"
#include "USDMaterialXMaterializer.h"
#include "USDStageBridge.h"
#include "USDConnectProtocol.h"
#include "TxnBuilder.h"

#include "USDStageActor.h"
#include "USDListener.h"
#include "EngineUtils.h"
#include "Logging/LogMacros.h"
#include "Stats/Stats.h"
#include "Misc/Guid.h"
#include "Misc/App.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/Crc.h"
#include "Misc/ScopeLock.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"

#include <algorithm>
#include <type_traits>

#if WITH_EDITOR
#include "ScopedTransaction.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogUSDConnectSubsystem, Log, All);

DECLARE_STATS_GROUP(TEXT("OpenUSDConnect"), STATGROUP_OpenUSDConnect, STATCAT_Advanced);
DECLARE_CYCLE_STAT(TEXT("USDConnect Tick"), STAT_USDConnectTick, STATGROUP_OpenUSDConnect);

namespace ClientCore = openusdconnect::client;

// The latest captured value per prim, and per input of a prim.
struct FUSDConnectCapturedEdits
{
	TMap<FString, FEmitXformTrs> Xforms;
	TMap<FString, FEmitVisibility> Visibilities;
	TMap<FString, FEmitConnectableInput> Inputs;
};

namespace
{
ClientCore::TimePoint Now()
{
	return std::chrono::steady_clock::now();
}

EUSDConnectRecoveryDisposition
ToRecoveryDisposition(ClientCore::ProducerRecoveryDisposition Disposition)
{
	switch (Disposition)
	{
	case ClientCore::ProducerRecoveryDisposition::RecoverableConflict:
		return EUSDConnectRecoveryDisposition::RecoverableConflict;
	case ClientCore::ProducerRecoveryDisposition::InvalidOperation:
		return EUSDConnectRecoveryDisposition::InvalidOperation;
	case ClientCore::ProducerRecoveryDisposition::SessionFatal:
		return EUSDConnectRecoveryDisposition::SessionFatal;
	case ClientCore::ProducerRecoveryDisposition::None:
		break;
	}
	return EUSDConnectRecoveryDisposition::None;
}

bool TargetsEndpoint(const ClientCore::ProducerConfig& Config, const FString& Host, int32 Port,
					 const FString& Department)
{
	return Config.Host == OUC::ToUtf8(Host) && Config.Port == Port &&
		   Config.Department == OUC::ToUtf8(Department);
}
} // namespace

// ---------------------------------------------------------------------------
// Authentication helpers
// ---------------------------------------------------------------------------
static const TCHAR* OUCAuthConfigSection = TEXT("OpenUSDConnect.Tokens");

static FString MakeAuthConfigKey(const FString& Host, int32 Port, const FString& Department)
{
	FString Key = FString::Printf(TEXT("%s:%d:%s"), *Host, Port, *Department);
	Key.ReplaceInline(TEXT("\\"), TEXT("_"));
	Key.ReplaceInline(TEXT("/"), TEXT("_"));
	Key.ReplaceInline(TEXT(" "), TEXT("_"));
	return Key;
}

// ---------------------------------------------------------------------------
// UWorldSubsystem lifecycle
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
	Super::Initialize(Collection);
	bSuppressEmit.store(false);
	ReceiverNotifications = MakeShared<ClientCore::NotificationQueue>();
	ProducerNotifications = MakeShared<ClientCore::NotificationQueue>();
	CapturedEdits = MakeShared<FUSDConnectCapturedEdits>();

	// Generate the stable client ID. Producer session identity is endpoint-scoped
	// and is created lazily when ConnectResolved selects an endpoint.
	{
		const FString MachineId = FPlatformProcess::ComputerName();
		const FString ProjectId = FApp::GetProjectName();
		const FString Combined = MachineId + ProjectId;
		const uint32 Hash = FCrc::MemCrc32(TCHAR_TO_UTF8(*Combined), Combined.Len());
		ClientId = FString::Printf(TEXT("unreal-%08x-%s"), Hash, *MachineId);
	}

	// IMPORTANT: don't call Connect() here. Spawning FRunnableThreads from inside
	// a UWorldSubsystem::Initialize() runs mid-world-load and can stall editor
	// startup. Instead defer to the first Tick() that runs after the world is
	// fully initialized.
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	bPendingAutoConnect = (Settings && Settings->bAutoConnect);
}

void UUSDConnectSubsystem::Deinitialize()
{
	Disconnect();
	ReleaseProducer();
	Super::Deinitialize();
}

// ---------------------------------------------------------------------------
// Connect / Disconnect
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::Connect()
{
	ConnectResolved(false);
}

void UUSDConnectSubsystem::ReleaseReceiver()
{
	if (ReceiverRunner)
	{
		ReceiverRunner->StopAndWait();
		ReceiverRunner.Reset();
	}
	Receiver.Reset();
}

void UUSDConnectSubsystem::ReleaseProducer()
{
	if (ProducerRunner)
	{
		ProducerRunner->StopAndWait();
		ProducerRunner.Reset();
	}
	Producer.Reset();
}

void UUSDConnectSubsystem::StopClients()
{
	ReleaseReceiver();
	if (Producer)
	{
		if (bActiveEmitterStarted && !Flush(2.0f))
		{
			UE_LOG(LogUSDConnectSubsystem, Warning,
				   TEXT("Disconnecting with %llu unacknowledged producer transactions"),
				   static_cast<uint64>(Producer->Status().PendingTransactions));
		}
		Producer->Disconnect();
		ProducerRunner->Wake();
	}
	DeliverNotifications();
	EmittedXformPrims.Reset();

	ActiveServerHost.Empty();
	ActiveServerPort = 0;
	bActiveReceiverStarted = false;
	bActiveEmitterStarted = false;
	bActiveUsingLiveMetadata = false;
	bDeferredEmitterForToken = false;
	ActiveSnapshotSeq = 0;
	SetAuthToken(FString());
}

void UUSDConnectSubsystem::ConnectResolved(bool bRespectLiveMetadataAutoStart)
{
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings)
		return;

	FString TargetHost = Settings->ServerHost;
	int32 TargetPort = Settings->ServerPort;
	int32 ReceiverInitialLastSeq = 0;
	bool bStartReceiver = true;
	bool bStartEmitter = true;
	bool bUsingLiveMetadata = false;
	bool bTargetRequiresToken = false;

	if (Settings->bUseLiveMetadataFromStage)
	{
		if (AUsdStageActor* StageActor = CachedStageActor.Get())
		{
			FUSDLiveOpenMetadata Metadata;
			if (FUSDStageBridge::ReadLiveOpenMetadata(StageActor, Metadata))
			{
				TargetHost = Metadata.Host;
				TargetPort = Metadata.Port;
				ReceiverInitialLastSeq = FMath::Max(0, Metadata.SnapshotSeq);
				bUsingLiveMetadata = true;
				bTargetRequiresToken = Metadata.bRequiresToken;
				if (bRespectLiveMetadataAutoStart)
				{
					bStartReceiver = Settings->bAutoStartReceiverFromLiveMetadata;
					bStartEmitter = Settings->bAutoStartEmitterFromLiveMetadata;
				}
			}
		}
	}

	if (bStartReceiver && !Settings->Department.IsEmpty())
	{
		StopClients();
		SetStatusMessage(TEXT("unsupported_configuration"),
						 TEXT("Department receive mode requires managed layered replay, which the "
							  "native Unreal plugin does not implement"));
		UE_LOG(LogUSDConnectSubsystem, Error,
			   TEXT("Department '%s' rejected: native Unreal receivers do not support managed "
					"layered replay"),
			   *Settings->Department);
		return;
	}

	const bool bSameProducerEndpoint =
		Producer &&
		TargetsEndpoint(Producer->Configuration(), TargetHost, TargetPort, Settings->Department);
	if (Producer && !bSameProducerEndpoint)
	{
		const uint64 Pending = Producer->Status().PendingTransactions;
		if (Pending > 0)
		{
			SetStatusMessage(
				TEXT("pending_transactions"),
				FString::Printf(TEXT("Cannot switch endpoints with %llu unacknowledged "
									 "transaction(s); reconnect to %s:%d and flush first"),
								Pending, *OUC::ToFString(Producer->Configuration().Host),
								static_cast<int32>(Producer->Configuration().Port)));
			return;
		}
	}
	if (bSameProducerEndpoint)
	{
		if (const std::optional<ClientCore::TransactionFailure> Failure = Producer->Failure())
		{
			SetStatusMessage(TEXT("recovery_required"), OUC::ToFString(Failure->Describe()));
			return;
		}
	}

	const FString TargetToken = Settings->bPersistAuthTokens
									? LoadAuthToken(TargetHost, TargetPort, Settings->Department)
									: FString();
	const bool bDelayEmitterForToken =
		bTargetRequiresToken && TargetToken.IsEmpty() && bStartReceiver && bStartEmitter;

	if (!bStartReceiver && !bStartEmitter)
	{
		StopClients();
		ActiveServerHost = TargetHost;
		ActiveServerPort = TargetPort;
		bActiveUsingLiveMetadata = bUsingLiveMetadata;
		ActiveSnapshotSeq = ReceiverInitialLastSeq;
		SetAuthToken(TargetToken);
		SetStatusMessage(bTargetRequiresToken ? TEXT("token_required") : TEXT("not_connected"),
						 TEXT("Live metadata configured; auto-start disabled"));
		UE_LOG(LogUSDConnectSubsystem, Log,
			   TEXT("OpenUSDConnect live metadata configured %s:%d, auto-start disabled"),
			   *TargetHost, TargetPort);
		return;
	}

	if ((Receiver || bActiveEmitterStarted) && ActiveServerHost == TargetHost &&
		ActiveServerPort == TargetPort && bActiveReceiverStarted == bStartReceiver &&
		bActiveEmitterStarted == bStartEmitter)
	{
		UE_LOG(LogUSDConnectSubsystem, Warning, TEXT("Already connected"));
		return;
	}

	StopClients();
	const std::chrono::milliseconds ReconnectDelay(
		FMath::Max<int64>(1, FMath::RoundToInt64(Settings->ReconnectDelaySecs * 1000.0)));
	const TFunction<FString()> ReadToken = [this]
	{
		return ReadAuthToken();
	};
	if (!bSameProducerEndpoint)
	{
		ReleaseProducer();
		ClientCore::ProducerConfig ProducerSettings;
		ProducerSettings.Host = OUC::ToUtf8(TargetHost);
		ProducerSettings.Port = static_cast<uint16>(TargetPort);
		ProducerSettings.ClientId = OUC::ToUtf8(ClientId);
		ProducerSettings.SessionId = OUC::ToUtf8(FGuid::NewGuid().ToString(EGuidFormats::Digits));
		ProducerSettings.Origin = ProducerSettings.SessionId;
		ProducerSettings.Department = OUC::ToUtf8(Settings->Department);
		ProducerSettings.ReconnectBaseDelay = ReconnectDelay;
		ProducerSettings.ReconnectMaxDelay =
			std::max(ProducerSettings.ReconnectMaxDelay, ReconnectDelay);
		if (TargetPort < 1 || TargetPort > MAX_uint16 ||
			!ClientCore::ProducerEndpoint::IsValidConfiguration(ProducerSettings))
		{
			SetStatusMessage(TEXT("error"), FString::Printf(TEXT("Invalid server endpoint %s:%d"),
															*TargetHost, TargetPort));
			return;
		}
		Producer = MakeShared<ClientCore::ProducerEndpoint>(MoveTemp(ProducerSettings),
															*ProducerNotifications);
		ProducerRunner = MakeShared<FEndpointRunner<ClientCore::ProducerEndpoint>>(
			*Producer, TEXT("Emitter"), ReadToken);
		if (!ProducerRunner->Start())
		{
			ReleaseProducer();
			SetStatusMessage(TEXT("error"), TEXT("Failed to start the emitter thread"));
			return;
		}
	}
	UE_LOG(LogUSDConnectSubsystem, Log, TEXT("Connecting to %s:%d (client_id=%s)"), *TargetHost,
		   TargetPort, *ClientId);
	if (bUsingLiveMetadata)
	{
		UE_LOG(LogUSDConnectSubsystem, Log,
			   TEXT("Using USD live metadata; receiver will sync from seq=%d"),
			   ReceiverInitialLastSeq + 1);
	}

	ActiveServerHost = TargetHost;
	ActiveServerPort = TargetPort;
	bActiveReceiverStarted = false;
	bActiveEmitterStarted = false;
	bActiveUsingLiveMetadata = bUsingLiveMetadata;
	bDeferredEmitterForToken = bDelayEmitterForToken;
	ActiveSnapshotSeq = ReceiverInitialLastSeq;
	SetAuthToken(TargetToken);
	SetStatusMessage(bTargetRequiresToken && TargetToken.IsEmpty() ? TEXT("token_required")
																   : TEXT("connecting"),
					 bDelayEmitterForToken ? TEXT("Starting receiver first to obtain auth token")
										   : TEXT("Connecting receiver/emitter"));

	if (bStartReceiver)
	{
		ClientCore::ReceiverConfig ReceiverSettings;
		ReceiverSettings.Host = OUC::ToUtf8(TargetHost);
		ReceiverSettings.Port = static_cast<uint16>(TargetPort);
		ReceiverSettings.ClientId = OUC::ToUtf8(ClientId);
		ReceiverSettings.Origin = Producer->Configuration().SessionId;
		ReceiverSettings.Department = OUC::ToUtf8(Settings->Department);
		ReceiverSettings.LayeredReplay = false;
		ReceiverSettings.SyncFrom = ReceiverInitialLastSeq + 1;
		ReceiverSettings.ReconnectBaseDelay = ReconnectDelay;
		ReceiverSettings.ReconnectMaxDelay =
			std::max(ReceiverSettings.ReconnectMaxDelay, ReconnectDelay);
		Receiver = MakeShared<ClientCore::ReceiverEndpoint>(MoveTemp(ReceiverSettings),
															*ReceiverNotifications);
		ReceiverRunner = MakeShared<FEndpointRunner<ClientCore::ReceiverEndpoint>>(
			*Receiver, TEXT("Receiver"), ReadToken);
		static_cast<void>(Receiver->Start(Now()));
		if (ReceiverRunner->Start())
		{
			bActiveReceiverStarted = true;
		}
		else
		{
			ReleaseReceiver();
		}
	}

	if (bStartEmitter && !bDelayEmitterForToken)
	{
		bActiveEmitterStarted = true;
		// Unlike Tick's requests, an explicit connect also clears a handshake rejection.
		const ClientCore::TimePoint Current = Now();
		if (Producer->Connect(Current, Current + Producer->Configuration().HandshakeTimeout) ==
			ClientCore::ConnectResult::Started)
		{
			ProducerRunner->Wake();
		}
	}
}

void UUSDConnectSubsystem::Disconnect()
{
	DetachFromStageActor();
	StopClients();
	CachedStageActor = nullptr;
	LastLiveMetadataKey.Empty();
	SetStatusMessage(TEXT("not_connected"), TEXT("Disconnected"));
}

bool UUSDConnectSubsystem::Flush(float TimeoutSeconds) const
{
	if (!Producer)
	{
		return true;
	}
	const double Deadline = FPlatformTime::Seconds() + FMath::Max(0.0f, TimeoutSeconds);
	for (;;)
	{
		const ClientCore::ProducerStatus ProducerState = Producer->Status();
		if (ProducerState.Failure || ProducerState.PendingTransactions == 0)
		{
			return !ProducerState.Failure;
		}
		if (FPlatformTime::Seconds() >= Deadline)
		{
			return false;
		}
		// The game thread waits here, so Tick cannot reconnect the producer.
		if (bActiveEmitterStarted)
		{
			RequestProducerConnect();
		}
		FPlatformProcess::Sleep(0.005f);
	}
}

void UUSDConnectSubsystem::RefreshLiveMetadataFromStage(AUsdStageActor* Actor)
{
	const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
	if (!Settings || !Settings->bUseLiveMetadataFromStage)
		return;

	FUSDLiveOpenMetadata Metadata;
	if (!FUSDStageBridge::ReadLiveOpenMetadata(Actor, Metadata))
		return;

	const FString MetadataKey = Metadata.MakeKey();
	if (MetadataKey == LastLiveMetadataKey)
		return;

	LastLiveMetadataKey = MetadataKey;
	UE_LOG(LogUSDConnectSubsystem, Log,
		   TEXT("Detected OpenUSDConnect live metadata on stage: %s:%d snapshot_seq=%d vfs_url=%s"),
		   *Metadata.Host, Metadata.Port, Metadata.SnapshotSeq, *Metadata.VfsUrl);

	if (Settings->bAutoConnect)
	{
		bPendingAutoConnect = false;
		ConnectResolved(true);
	}
}

bool UUSDConnectSubsystem::IsConnected() const
{
	return Receiver && Receiver->Status().Connected;
}

FUSDConnectStatus UUSDConnectSubsystem::GetStatus() const
{
	FUSDConnectStatus Status;
	Status.EndpointHost = ActiveServerHost;
	Status.EndpointPort = ActiveServerPort;
	Status.bUsingLiveMetadata = bActiveUsingLiveMetadata;
	Status.SnapshotSeq = ActiveSnapshotSeq;
	Status.bReceiverStarted = bActiveReceiverStarted;
	Status.bEmitterStarted = bActiveEmitterStarted;
	if (Receiver)
	{
		const ClientCore::ReceiverStatus ReceiverState = Receiver->Status();
		Status.bReceiverConnected = ReceiverState.Connected;
		Status.bReceiverSynchronized = ReceiverState.Synchronized;
	}
	{
		FScopeLock Lock(&StatusCS);
		Status.AuthState = LastAuthState;
		Status.LastMessage = LastStatusMessage;
	}
	if (Producer)
	{
		const ClientCore::ProducerStatus ProducerState = Producer->Status();
		Status.bEmitterConnected = ProducerState.Connected;
		Status.SubmittedTransactions = static_cast<int64>(ProducerState.NextTransactionId - 1);
		Status.AcknowledgedTransactions =
			static_cast<int64>(ProducerState.AcknowledgedTransactions);
		Status.PendingTransactions = static_cast<int32>(
			FMath::Min<uint64>(ProducerState.PendingTransactions, static_cast<uint64>(MAX_int32)));
		if (ProducerState.Failure)
		{
			Status.bRecoveryRequired = true;
			Status.RecoveryDisposition =
				ToRecoveryDisposition(ProducerState.Failure->Disposition());
			Status.AuthState = TEXT("recovery_required");
			Status.LastMessage = OUC::ToFString(ProducerState.Failure->Describe());
		}
	}
	return Status;
}

FString UUSDConnectSubsystem::LoadAuthToken(const FString& Host, int32 Port,
											const FString& Department) const
{
	FString Token;
	if (GConfig)
	{
		GConfig->GetString(OUCAuthConfigSection, *MakeAuthConfigKey(Host, Port, Department), Token,
						   GGameUserSettingsIni);
	}
	return Token;
}

void UUSDConnectSubsystem::SaveAuthToken(const FString& Host, int32 Port, const FString& Department,
										 const FString& Token) const
{
	if (!GConfig || Token.IsEmpty())
		return;
	GConfig->SetString(OUCAuthConfigSection, *MakeAuthConfigKey(Host, Port, Department), *Token,
					   GGameUserSettingsIni);
	GConfig->Flush(false, GGameUserSettingsIni);
}

void UUSDConnectSubsystem::SetStatusMessage(const FString& AuthState, const FString& Message)
{
	FScopeLock Lock(&StatusCS);
	LastAuthState = AuthState;
	LastStatusMessage = Message;
}

FString UUSDConnectSubsystem::ReadAuthToken() const
{
	FScopeLock Lock(&AuthTokenCS);
	return ActiveAuthToken;
}

void UUSDConnectSubsystem::SetAuthToken(const FString& Token)
{
	FScopeLock Lock(&AuthTokenCS);
	ActiveAuthToken = Token;
}

void UUSDConnectSubsystem::DeliverNotifications()
{
	for (const bool bProducer : {false, true})
	{
		ClientCore::NotificationQueue& Queue =
			bProducer ? *ProducerNotifications : *ReceiverNotifications;
		const TCHAR* Role = bProducer ? TEXT("Emitter") : TEXT("Receiver");
		for (const ClientCore::Notification& Notification : Queue.Drain())
		{
			std::visit(
				[this, bProducer, Role](const auto& Value)
				{
					using FKind = std::decay_t<decltype(Value)>;
					if constexpr (std::is_same_v<FKind, ClientCore::TokenIssued>)
					{
						const FString Token = OUC::ToFString(Value.Token);
						SetAuthToken(Token);
						const UUSDConnectSettings* Settings = GetDefault<UUSDConnectSettings>();
						const bool bPersisted = Settings && Settings->bPersistAuthTokens;
						if (bPersisted)
						{
							SaveAuthToken(ActiveServerHost, ActiveServerPort, Settings->Department,
										  Token);
						}
						SetStatusMessage(bPersisted ? TEXT("token_saved") : TEXT("token_issued"),
										 bPersisted ? TEXT("Auth token issued and saved")
													: TEXT("Auth token issued for this session"));
						if (bDeferredEmitterForToken)
						{
							bDeferredEmitterForToken = false;
							bActiveEmitterStarted = true;
							SetStatusMessage(TEXT("connected"),
											 TEXT("Auth token available; emitter started"));
						}
					}
					else if constexpr (std::is_same_v<FKind, ClientCore::Connected>)
					{
						if (bProducer)
						{
							// A replacement server has no guarantee that it saw prerequisites from
							// the previous connection, so each prim's next transform carries them
							// again. A captured edit is newer than the current values and wins.
							AUsdStageActor* StageActor = CachedStageActor.Get();
							for (const FString& PrimPath : EmittedXformPrims)
							{
								if (IsValid(StageActor) &&
									!CapturedEdits->Xforms.Contains(PrimPath) &&
									!CapturedEdits->Visibilities.Contains(PrimPath))
								{
									CapturePrim(StageActor, PrimPath);
								}
							}
							EmittedXformPrims.Reset();
						}
						SetStatusMessage(TEXT("connected"),
										 FString::Printf(TEXT("%s connected to %s:%d"), Role,
														 *ActiveServerHost, ActiveServerPort));
					}
					else if constexpr (std::is_same_v<FKind, ClientCore::Disconnected>)
					{
						SetStatusMessage(TEXT("not_connected"),
										 FString::Printf(TEXT("%s disconnected from %s:%d"), Role,
														 *ActiveServerHost, ActiveServerPort));
					}
					else if constexpr (std::is_same_v<FKind, ClientCore::HandshakeRejected>)
					{
						const FString Reason = OUC::ToFString(Value.Reason);
						if (Value.Authentication)
						{
							SetStatusMessage(
								TEXT("auth_rejected"),
								FString::Printf(TEXT("%s auth rejected: %s"), Role, *Reason));
						}
						else
						{
							const OpenUSDConnect::HelloRejectionCode Code = Value.Code;
							SetStatusMessage(
								Code == OpenUSDConnect::HelloRejectionCode::Unspecified
									? FString(TEXT("connection_rejected"))
									: FString(UTF8_TO_TCHAR(
										  OpenUSDConnect::EnumNameHelloRejectionCode(Code))),
								FString::Printf(TEXT("%s connection rejected: %s"), Role, *Reason));
						}
					}
					// Stage metadata and playback notifications have no consumer in Unreal.
				},
				Notification);
		}
	}
}

void UUSDConnectSubsystem::RequestProducerConnect() const
{
	const ClientCore::TimePoint Current = Now();
	if (Producer->RequestConnect(Current, Current + Producer->Configuration().HandshakeTimeout))
	{
		ProducerRunner->Wake();
	}
}

void UUSDConnectSubsystem::RequestReceiverReplay(const FString& Reason)
{
	SetStatusMessage(TEXT("receiver_recovering"), Reason);
	verify(Receiver->RequestReplayFrom(Receiver->Status().LastAppliedSequence + 1));
	ReceiverRunner->Wake();
}

// ---------------------------------------------------------------------------
// Tick
// ---------------------------------------------------------------------------

TStatId UUSDConnectSubsystem::GetStatId() const
{
	RETURN_QUICK_DECLARE_CYCLE_STAT(UUSDConnectSubsystem, STATGROUP_OpenUSDConnect);
}

void UUSDConnectSubsystem::Tick(float DeltaTime)
{
	SCOPE_CYCLE_COUNTER(STAT_USDConnectTick);

	// Guard: don't touch anything until the world is fully initialized and not
	// being torn down. UTickableWorldSubsystem can tick during the tail end of
	// world load running heavy work here can deadlock startup.
	UWorld* World = GetWorld();
	if (!World || !World->bIsWorldInitialized || World->bIsTearingDown)
	{
		return;
	}

	// Attach to stage actor if not already done (handles late spawning / PIE).
	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		StageActor = FindStageActor();
		if (StageActor)
		{
			AttachToStageActor(StageActor);
		}
	}
	if (StageActor)
	{
		RefreshLiveMetadataFromStage(StageActor);
		QueueInitialMaterializations(StageActor);
	}

	// Perform deferred auto-connect after stage metadata has had a chance to
	// override the project-default endpoint.
	if (bPendingAutoConnect)
	{
		bPendingAutoConnect = false;
		ConnectResolved(true);
	}

	// Local edits are read before received frames apply, so a replay cannot
	// replace them with older server values.
	if (StageActor)
	{
		CaptureEdits(StageActor);
	}

	DrainAndApply();

	// Token, connection, and rejection reports from both runner threads.
	DeliverNotifications();
	if (Producer && bActiveEmitterStarted)
	{
		RequestProducerConnect();
	}

	// Emit any user edits captured by the USD notice listener since last tick.
	DrainAndEmit();

	// Refresh .mtlx documents for materials whose networks changed this tick
	// (received or local edits) so the engine's MaterialX rendering follows.
	ProcessPendingMaterializations();
}

// ---------------------------------------------------------------------------
// Stage actor attachment
// ---------------------------------------------------------------------------

AUsdStageActor* UUSDConnectSubsystem::FindStageActor() const
{
	// Picks the first AUsdStageActor in the world. With multiple stage actors
	// (e.g. one per layer file), only the first one is live-synced. See the
	// "Single stage actor" entry in PLUGIN_DEV.md.
	UWorld* World = GetWorld();
	if (!World)
		return nullptr;
	for (TActorIterator<AUsdStageActor> It(World); It; ++It)
	{
		if (*It)
			return *It;
	}
	return nullptr;
}

void UUSDConnectSubsystem::AttachToStageActor(AUsdStageActor* Actor)
{
	DetachFromStageActor();
	CachedStageActor = Actor;

	// Subscribe to the engine's USD notice wrapper. Its delegate fires with
	// exact SdfPaths (info-changes + resyncs), unlike OnPrimChanged which
	// rolls every change up to the nearest KindsToCollapse ancestor.
	FUsdListener& Listener = Actor->GetUsdListener();
	ObjectsChangedHandle = Listener.GetOnObjectsChanged().AddLambda(
		[this](const UsdUtils::FObjectChangesByPath& InfoChanges,
			   const UsdUtils::FObjectChangesByPath& ResyncChanges)
		{
			// Echo guard: drop notices generated by our own DrainAndApply.
			if (bSuppressEmit.load())
				return;

			FScopeLock Lock(&PendingEmitPathsCS);
			auto Collect = [this](const UsdUtils::FObjectChangesByPath& Map)
			{
				for (const auto& Pair : Map)
				{
					const FString& Path = Pair.Key;
					if (Path.IsEmpty() || Path == TEXT("/"))
						continue;

					// Strip property suffix ".attrName" we want the prim path.
					// Changed "inputs:*" properties keep their name so the drain
					// can emit just the edited shader inputs.
					int32 DotIdx = INDEX_NONE;
					if (Path.FindChar(TEXT('.'), DotIdx))
					{
						FString PrimPath = Path.Left(DotIdx);
						FString PropName = Path.RightChop(DotIdx + 1);
						if (PropName.StartsWith(TEXT("inputs:")))
						{
							PendingEmitInputs.FindOrAdd(PrimPath).Add(MoveTemp(PropName));
						}
						PendingEmitPaths.Add(MoveTemp(PrimPath));
					}
					else
					{
						PendingEmitPaths.Add(Path);
					}
				}
			};
			Collect(InfoChanges);
			Collect(ResyncChanges);
		});

	UE_LOG(LogUSDConnectSubsystem, Log,
		   TEXT("Attached to AUsdStageActor (%s) subscribed to FUsdListener::OnObjectsChanged"),
		   *Actor->GetName());
}

void UUSDConnectSubsystem::DetachFromStageActor()
{
	if (AUsdStageActor* Actor = CachedStageActor.Get())
	{
		if (ObjectsChangedHandle.IsValid())
		{
			Actor->GetUsdListener().GetOnObjectsChanged().Remove(ObjectsChangedHandle);
		}
	}
	ObjectsChangedHandle.Reset();

	{
		FScopeLock Lock(&PendingEmitPathsCS);
		PendingEmitPaths.Reset();
		PendingEmitInputs.Reset();
	}

	*CapturedEdits = FUSDConnectCapturedEdits();
	EmittedXformPrims.Reset();
	CachedStageActor = nullptr;
	LastMaterializedRootLayerIdentifier.Empty();
}

// ---------------------------------------------------------------------------
// DrainAndApply (receiver → USD stage)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::DrainAndApply()
{
	if (!Receiver)
	{
		return;
	}

	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		constexpr size_t MaxBufferedFrames = 5000;
		if (Receiver->Status().QueuedFrames > MaxBufferedFrames)
		{
			RequestReceiverReplay(
				TEXT("Receiver queue overflowed before a USD stage was available; replay requested "
					 "from the last applied sequence"));
		}
		return;
	}

	constexpr int32 MaxApplyPerTick = 512;
	constexpr double MaxApplySecondsPerTick = 0.016; // 16 ms preserves ~60 fps

	const double Start = FPlatformTime::Seconds();
	int32 Applied = 0;
	FString FailureReason;

	bSuppressEmit.store(true);
	{
		TUniquePtr<FUSDEventChangeBlock> RunBlock;
		while (Applied < MaxApplyPerTick &&
			   FPlatformTime::Seconds() - Start <= MaxApplySecondsPerTick)
		{
			// One frame per drain, so the time budget never strands drained frames.
			const uint64 Generation = Receiver->Generation();
			const std::vector<std::vector<uint8>> Drained = Receiver->DrainFrames(1);
			if (Drained.empty())
			{
				break;
			}
			++Applied;
			const std::vector<uint8>& Frame = Drained.front();
			// The endpoint verified every queued envelope.
			const OpenUSDConnect::Envelope* Envelope = OpenUSDConnect::GetEnvelope(Frame.data());
			if (Envelope->payload_type() == OpenUSDConnect::Payload::Resync)
			{
				RunBlock.Reset();
				Receiver->ResetAppliedProgress();
				continue;
			}
			const OpenUSDConnect::BroadcastEvent* Broadcast = Envelope->payload_as_BroadcastEvent();
			if (!Broadcast)
			{
				continue;
			}
			const int32 Seq = Broadcast->seq();
			const OpenUSDConnect::EventPayload EventKind = Broadcast->event()->event_type();
			if (FUSDEventApplier::EventUsesChangeBlock(EventKind))
			{
				if (!RunBlock)
				{
					RunBlock = MakeUnique<FUSDEventChangeBlock>();
				}
			}
			else
			{
				RunBlock.Reset();
			}
			FString TouchedPrim;
			if (!FUSDEventApplier::ApplyValidatedFrame(
					MakeArrayView(Frame.data(), static_cast<int32>(Frame.size())), StageActor,
					&TouchedPrim))
			{
				RunBlock.Reset();
				FailureReason = FString::Printf(TEXT("Failed to apply receiver sequence %d"), Seq);
				break;
			}
			// False only when a reconnect replaced the stream, which then resumes
			// from its own cursor.
			static_cast<void>(Receiver->MarkAppliedThrough(Generation, Seq));
			// Received network edits dirty their owning material for the
			// materializer. EnsurePrim is included because shader-node
			// creation changes the network without a connectable event.
			if (!TouchedPrim.IsEmpty() &&
				(EventKind == OpenUSDConnect::EventPayload::SetConnectableInput ||
				 EventKind == OpenUSDConnect::EventPayload::SetConnectableConnection ||
				 EventKind == OpenUSDConnect::EventPayload::EnsurePrim))
			{
				PendingMaterializePrims.Add(MoveTemp(TouchedPrim));
			}
		}
	}
	bSuppressEmit.store(false);
	if (!FailureReason.IsEmpty())
	{
		RequestReceiverReplay(FailureReason);
		return;
	}

	if (Receiver->MarkReplayApplied())
	{
		const ClientCore::ReceiverStatus ReceiverState = Receiver->Status();
		UE_LOG(LogUSDConnectSubsystem, Log,
			   TEXT("Receiver replay applied through seq=%d epoch=%llu publishing enabled"),
			   ReceiverState.ReplayHeadSequence, static_cast<uint64>(ReceiverState.ReplayEpoch));
		ReceiverRunner->Wake();
	}
	if (Applied > 0)
	{
		UE_LOG(LogUSDConnectSubsystem, Verbose,
			   TEXT("Applied %d frame(s) this tick (queue remaining: %llu)"), Applied,
			   static_cast<uint64>(Receiver->Status().QueuedFrames));
	}
}

// ---------------------------------------------------------------------------
// CaptureEdits and DrainAndEmit (emitter ← USD stage, via TfNotice listener)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::CaptureEdits(AUsdStageActor* StageActor)
{
	TSet<FString> Changed;
	TMap<FString, TSet<FString>> ChangedInputs;
	{
		FScopeLock Lock(&PendingEmitPathsCS);
		if (PendingEmitPaths.Num() == 0 && PendingEmitInputs.Num() == 0)
			return;
		Changed = MoveTemp(PendingEmitPaths);
		ChangedInputs = MoveTemp(PendingEmitInputs);
		PendingEmitPaths.Reset();
		PendingEmitInputs.Reset();
	}

	UE_LOG(LogUSDConnectSubsystem, Verbose,
		   TEXT("Capturing %d changed prim path(s) from FUsdListener"), Changed.Num());

	for (const FString& Path : Changed)
	{
		CapturePrim(StageActor, Path);
	}
	for (const auto& Pair : ChangedInputs)
	{
		// Edits on a Material's document-projected interface inputs are
		// local artifacts; reroute them onto the inline shader instead of
		// emitting an orphan material-level event. The shader authoring
		// re-enters this path next tick and is captured normally.
		if (FUSDMaterialXMaterializer::RerouteMaterialInterfaceEdit(StageActor, Pair.Key,
																	Pair.Value))
		{
			continue;
		}
		FEmitConnectableInput Event;
		if (FUSDStageBridge::ReadConnectableInputs(StageActor, Pair.Key, Pair.Value, Event))
		{
			FEmitConnectableInput& Captured = CapturedEdits->Inputs.FindOrAdd(Pair.Key);
			Captured.PrimPath = MoveTemp(Event.PrimPath);
			Captured.InfoId = MoveTemp(Event.InfoId);
			for (FEmitConnectableValue& Value : Event.Inputs)
			{
				FEmitConnectableValue* Existing = Captured.Inputs.FindByPredicate(
					[&Value](const FEmitConnectableValue& Candidate)
					{
						return Candidate.Name == Value.Name;
					});
				if (Existing)
				{
					*Existing = MoveTemp(Value);
				}
				else
				{
					Captured.Inputs.Add(MoveTemp(Value));
				}
			}
		}
		// Local shader edits also dirty their owning material so the
		// materializer refreshes the local .mtlx document.
		PendingMaterializePrims.Add(Pair.Key);
	}
}

void UUSDConnectSubsystem::CapturePrim(AUsdStageActor* StageActor, const FString& PrimPath)
{
	FEmitXformTrs Xform;
	bool bFromMatrixOp = false;
	if (FUSDStageBridge::ReadXformTrs(StageActor, PrimPath, Xform, &bFromMatrixOp))
	{
		if (bFromMatrixOp)
		{
			// Suppress our own listener: the restore fires notices, but
			// it re-authors the exact values being captured.
			bSuppressEmit.store(true);
			FUSDStageBridge::RestoreCanonicalXformOps(StageActor, PrimPath, Xform);
			bSuppressEmit.store(false);
		}
		CapturedEdits->Xforms.Add(PrimPath, Xform);
	}

	FEmitVisibility Visibility;
	if (FUSDStageBridge::ReadVisibility(StageActor, PrimPath, Visibility))
	{
		CapturedEdits->Visibilities.Add(PrimPath, Visibility);
	}
}

void UUSDConnectSubsystem::DrainAndEmit()
{
	if (!Producer || !Receiver || bSuppressEmit.load())
		return;
	// New transactions wait until the receiver's replay is applied.
	if (!Producer->Status().Connected || !Receiver->Status().Synchronized)
		return;

	for (auto It = CapturedEdits->Xforms.CreateIterator(); It; ++It)
	{
		const TArray<FEmitXformTrs> Batch = {It.Value()};
		const bool bIncludeEnsureXformOps = !EmittedXformPrims.Contains(It.Key());
		if (SubmitTransaction(It.Key(), TEXT("TRS"), bIncludeEnsureXformOps ? 2 : 1,
							  [&](uint64 TxnId, std::vector<uint8>& Frame)
							  {
								  return BuildXformTxnFrame(TxnId, Batch, Frame,
															bIncludeEnsureXformOps) ==
										 ClientCore::ProtocolResult::Success;
							  }))
		{
			EmittedXformPrims.Add(It.Key());
			It.RemoveCurrent();
		}
	}
	for (auto It = CapturedEdits->Visibilities.CreateIterator(); It; ++It)
	{
		const TArray<FEmitVisibility> Batch = {It.Value()};
		if (SubmitTransaction(It.Key(), TEXT("visibility"), 1,
							  [&](uint64 TxnId, std::vector<uint8>& Frame)
							  {
								  return BuildVisibilityTxnFrame(TxnId, Batch, Frame) ==
										 ClientCore::ProtocolResult::Success;
							  }))
		{
			It.RemoveCurrent();
		}
	}
	for (auto It = CapturedEdits->Inputs.CreateIterator(); It; ++It)
	{
		const TArray<FEmitConnectableInput> Batch = {It.Value()};
		if (SubmitTransaction(It.Key(), TEXT("connectable input"), 1,
							  [&](uint64 TxnId, std::vector<uint8>& Frame)
							  {
								  return BuildConnectableInputTxnFrame(TxnId, Batch, Frame) ==
										 ClientCore::ProtocolResult::Success;
							  }))
		{
			It.RemoveCurrent();
		}
	}
}

bool UUSDConnectSubsystem::SubmitTransaction(
	const FString& PrimPath, const TCHAR* Kind, int32 EventCount,
	TFunctionRef<bool(uint64, std::vector<uint8>&)> BuildFrame)
{
	FScopeLock Lock(&SubmitCS);
	const uint64 TxnId = Producer->NextTransactionId();
	std::vector<uint8> Frame;
	if (!BuildFrame(TxnId, Frame))
	{
		UE_LOG(LogUSDConnectSubsystem, Error, TEXT("Failed to build the %s transaction for %s"),
			   Kind, *PrimPath);
		return false;
	}
	UE_LOG(LogUSDConnectSubsystem, Verbose, TEXT("Appending %s transaction %llu for %s (%d bytes)"),
		   Kind, TxnId, *PrimPath, static_cast<int32>(Frame.size()));
	if (Producer->Append(TxnId, MoveTemp(Frame), static_cast<size_t>(EventCount), std::string()) !=
		ClientCore::ProducerResult::Accepted)
	{
		UE_LOG(LogUSDConnectSubsystem, Warning,
			   TEXT("The emitter refused the %s transaction for %s"), Kind, *PrimPath);
		return false;
	}
	ProducerRunner->Wake();
	return true;
}

// ---------------------------------------------------------------------------
// Materialization (local .mtlx documents for MaterialX rendering)
// ---------------------------------------------------------------------------

void UUSDConnectSubsystem::QueueInitialMaterializations(AUsdStageActor* Actor)
{
	FString RootIdentifier;
	TArray<FString> Materials;
	if (!FUSDStageBridge::ReadMaterialXMaterials(Actor, RootIdentifier, Materials))
	{
		return;
	}
	if (RootIdentifier.IsEmpty() || RootIdentifier == LastMaterializedRootLayerIdentifier)
	{
		return;
	}

	LastMaterializedRootLayerIdentifier = RootIdentifier;

	if (Materials.IsEmpty())
	{
		return;
	}

	int32 Changed = 0;
	bSuppressEmit.store(true);
	{
#if WITH_EDITOR
		const FScopedTransaction Transaction(NSLOCTEXT("OpenUSDConnect", "InitialMaterialXRefresh",
													   "Prepare live MaterialX materials"));
#endif

		for (const FString& Material : Materials)
		{
			if (FUSDMaterialXMaterializer::MaterializeMaterial(Actor, Material))
			{
				++Changed;
			}
		}
	}

	const FName MaterialXRenderContext(TEXT("mtlx"));
	const bool bChangedRenderContext = Actor->RenderContext != MaterialXRenderContext;
	if (bChangedRenderContext)
	{
		// The materialization transaction has ended, so its accumulated USD
		// notices are fully processed before SetRenderContext reloads the stage.
		Actor->SetRenderContext(MaterialXRenderContext);
	}
	bSuppressEmit.store(false);

	UE_LOG(LogUSDConnectSubsystem, Log,
		   TEXT("Initial OpenUSDConnect MaterialX refresh scanned %d material(s), updated %d, "
				"render_context=%s"),
		   Materials.Num(), Changed,
		   bChangedRenderContext ? TEXT("mtlx (selected)") : TEXT("mtlx"));
}

void UUSDConnectSubsystem::ProcessPendingMaterializations()
{
	if (PendingMaterializePrims.IsEmpty())
	{
		return;
	}

	AUsdStageActor* StageActor = CachedStageActor.Get();
	if (!StageActor || !IsValid(StageActor))
	{
		PendingMaterializePrims.Reset();
		return;
	}

	TSet<FString> Sources = MoveTemp(PendingMaterializePrims);
	PendingMaterializePrims.Reset();

	TSet<FString> Materials;
	for (const FString& Path : Sources)
	{
		FString Material = FUSDMaterialXMaterializer::FindOwningMaterial(StageActor, Path);
		if (!Material.IsEmpty())
		{
			Materials.Add(MoveTemp(Material));
		}
	}
	if (Materials.IsEmpty())
	{
		return;
	}

	{
#if WITH_EDITOR
		const FScopedTransaction Transaction(NSLOCTEXT("OpenUSDConnect", "LiveMaterialXRefresh",
													   "Refresh live MaterialX materials"));
#endif

		// The session-layer authoring below fires stage notices; suppress our own
		// listener so they don't loop back into the emit path. The stage actor's
		// listener still sees them that's what triggers the re-import.
		bSuppressEmit.store(true);
		for (const FString& Material : Materials)
		{
			FUSDMaterialXMaterializer::MaterializeMaterial(StageActor, Material);
		}
	}
	bSuppressEmit.store(false);
}
