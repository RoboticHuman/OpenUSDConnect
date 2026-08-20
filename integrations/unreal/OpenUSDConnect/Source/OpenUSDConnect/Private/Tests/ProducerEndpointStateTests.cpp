// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#if WITH_DEV_AUTOMATION_TESTS

#include "EmitClient.h"
#include "Misc/AutomationTest.h"
#include "USDWireFraming.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FOpenUSDConnectProducerOutboxReconnectTest,
								 "OpenUSDConnect.Producer.OutboxSurvivesClientReplacement",
								 EAutomationTestFlags::EditorContext |
									 EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerOutboxReconnectTest::RunTest(const FString& Parameters)
{
	TSharedRef<FProducerEndpointState> State =
		MakeShared<FProducerEndpointState>(TEXT("127.0.0.1"), 7200, TEXT(""), TEXT("session-a"));
	TestTrue(TEXT("same endpoint matches"),
			 State->MatchesEndpoint(TEXT("127.0.0.1"), 7200, TEXT("")));
	TestFalse(TEXT("different port is a different producer endpoint"),
			  State->MatchesEndpoint(TEXT("127.0.0.1"), 7201, TEXT("")));
	uint64 FirstGeneration = 0;
	TestTrue(TEXT("first connection starts"), State->BeginConnection(FirstGeneration));
	FString Error;
	TestTrue(TEXT("first handshake is accepted"),
			 State->AcceptServerHighwater(FirstGeneration, 0, Error));

	FQueuedProducerTxn FirstClaim;
	{
		OUC::FWireFrame Frame;
		TestEqual(TEXT("test frame builds"),
				  OUC::BuildHelloFrame(TEXT("emitter"), 0, TEXT("client-a"), TEXT("session-a"),
									   TEXT(""), Frame),
				  openusdconnect::client::FrameResult::Success);
		TestTrue(TEXT("transaction one is accepted"),
				 State->EnqueueFrame(FirstGeneration, 1, MoveTemp(Frame)));
		TestEqual(TEXT("next transaction advances"), State->GetNextTransactionId(), uint64(2));
		TestEqual(TEXT("one transaction remains pending"), State->GetPendingTransactionCount(),
				  uint64(1));
		TestTrue(TEXT("first client claims the frame"),
				 State->ClaimNextUnsent(FirstGeneration, FirstClaim));
		TestEqual(TEXT("claimed transaction identity"), FirstClaim.TxnId, uint64(1));
		TestTrue(TEXT("claimed encoded frame remains owned by endpoint state"),
				 FirstClaim.Frame.IsValid());
	}

	// The subsystem marks the endpoint outbox unsent when one socket client
	// stops. A replacement object then claims the same identity and exact bytes.
	State->Disconnect(FirstGeneration);
	uint64 ReplacementGeneration = 0;
	TestTrue(TEXT("replacement connection starts"), State->BeginConnection(ReplacementGeneration));
	TestTrue(TEXT("replacement handshake is accepted"),
			 State->AcceptServerHighwater(ReplacementGeneration, 0, Error));
	FQueuedProducerTxn ReplacementClaim;
	{
		FEmitClient ReplacementClient(nullptr, TEXT("client-a"), State, 0.01f);
		TestTrue(TEXT("replacement client reclaims the pending frame"),
				 State->ClaimNextUnsent(ReplacementGeneration, ReplacementClaim));
		TestFalse(TEXT("zero-timeout flush reports outstanding durability"),
				  ReplacementClient.FlushPending(0.0));
	}
	TestEqual(TEXT("replacement keeps transaction identity"), ReplacementClaim.TxnId, uint64(1));
	TestTrue(TEXT("replacement keeps exact encoded bytes"),
			 ReplacementClaim.Frame == FirstClaim.Frame);

	State->RetireThrough(ReplacementGeneration, 1);
	TestEqual(TEXT("acknowledgement empties the outbox"), State->GetPendingTransactionCount(),
			  uint64(0));
	TestEqual(TEXT("acknowledgement counter advances"), State->GetAcknowledgedTransactionCount(),
			  uint64(1));
	FEmitClient AcknowledgedClient(nullptr, TEXT("client-a"), State, 0.01f);
	TestTrue(TEXT("flush succeeds once the endpoint outbox is acknowledged"),
			 AcknowledgedClient.FlushPending(0.0));
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FOpenUSDConnectProducerEndpointIsolationTest,
								 "OpenUSDConnect.Producer.EndpointIsolationAndHighwater",
								 EAutomationTestFlags::EditorContext |
									 EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerEndpointIsolationTest::RunTest(const FString& Parameters)
{
	FProducerEndpointState First(TEXT("server-a"), 7200, TEXT(""), TEXT("session-a"));
	uint64 FirstGeneration = 0;
	TestTrue(TEXT("first endpoint starts a connection"), First.BeginConnection(FirstGeneration));
	FString Error;
	TestTrue(TEXT("first endpoint accepts initial highwater"),
			 First.AcceptServerHighwater(FirstGeneration, 0, Error));
	OUC::FWireFrame Frame;
	TestEqual(TEXT("test frame builds"),
			  OUC::BuildHelloFrame(TEXT("emitter"), 0, TEXT("client-a"), TEXT("session-a"),
								   TEXT(""), Frame),
			  openusdconnect::client::FrameResult::Success);
	TestTrue(TEXT("first endpoint accepts transaction one"),
			 First.EnqueueFrame(FirstGeneration, 1, MoveTemp(Frame)));
	First.Disconnect(FirstGeneration);
	uint64 ReconnectGeneration = 0;
	TestTrue(TEXT("reconnect starts"), First.BeginConnection(ReconnectGeneration));
	TestTrue(TEXT("matching durable highwater is accepted"),
			 First.AcceptServerHighwater(ReconnectGeneration, 1, Error));
	First.Disconnect(ReconnectGeneration);
	uint64 RegressionGeneration = 0;
	TestTrue(TEXT("regression connection starts"), First.BeginConnection(RegressionGeneration));
	TestFalse(TEXT("durable highwater regression is rejected"),
			  First.AcceptServerHighwater(RegressionGeneration, 0, Error));
	TestTrue(TEXT("regression requires explicit recovery"), First.IsRecoveryRequired());

	FProducerEndpointState Second(TEXT("server-b"), 7200, TEXT(""), TEXT("session-b"));
	TestEqual(TEXT("new endpoint starts a new ordered session"), Second.GetNextTransactionId(),
			  uint64(1));
	TestFalse(TEXT("new endpoint does not inherit recovery state"), Second.IsRecoveryRequired());
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FOpenUSDConnectProducerRejectionDispositionTest,
								 "OpenUSDConnect.Producer.RejectionDisposition",
								 EAutomationTestFlags::EditorContext |
									 EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerRejectionDispositionTest::RunTest(const FString& Parameters)
{
	FProducerEndpointState Conflict(TEXT("server"), 7200, TEXT(""), TEXT("conflict"));
	uint64 ConflictGeneration = 0;
	TestTrue(TEXT("conflict session starts"), Conflict.BeginConnection(ConflictGeneration));
	FString Error;
	TestTrue(TEXT("conflict session handshake"),
			 Conflict.AcceptServerHighwater(ConflictGeneration, 0, Error));
	OUC::FWireFrame ConflictFrame;
	TestEqual(TEXT("conflict frame builds"),
			  OUC::BuildHelloFrame(TEXT("emitter"), 0, TEXT("client"), TEXT("conflict"), TEXT(""),
								   ConflictFrame),
			  openusdconnect::client::FrameResult::Success);
	TestTrue(TEXT("conflict transaction queued"),
			 Conflict.EnqueueFrame(ConflictGeneration, 1, MoveTemp(ConflictFrame)));
	Conflict.MarkRejected(ConflictGeneration, 1, 3, TEXT("obsolete layer graph"));
	TestEqual(TEXT("stale graph is recoverable"), Conflict.GetRecoveryDisposition(),
			  EUSDConnectRecoveryDisposition::RecoverableConflict);

	FProducerEndpointState Invalid(TEXT("server"), 7200, TEXT(""), TEXT("invalid"));
	uint64 InvalidGeneration = 0;
	TestTrue(TEXT("invalid session starts"), Invalid.BeginConnection(InvalidGeneration));
	TestTrue(TEXT("invalid session handshake"),
			 Invalid.AcceptServerHighwater(InvalidGeneration, 0, Error));
	OUC::FWireFrame InvalidFrame;
	TestEqual(TEXT("invalid frame builds"),
			  OUC::BuildHelloFrame(TEXT("emitter"), 0, TEXT("client"), TEXT("invalid"), TEXT(""),
								   InvalidFrame),
			  openusdconnect::client::FrameResult::Success);
	TestTrue(TEXT("invalid transaction queued"),
			 Invalid.EnqueueFrame(InvalidGeneration, 1, MoveTemp(InvalidFrame)));
	Invalid.MarkRejected(InvalidGeneration, 1, 4, TEXT("invalid event"));
	TestEqual(TEXT("malformed operation is an integration fault"), Invalid.GetRecoveryDisposition(),
			  EUSDConnectRecoveryDisposition::InvalidOperation);

	FProducerEndpointState Sequence(TEXT("server"), 7200, TEXT(""), TEXT("sequence"));
	uint64 SequenceGeneration = 0;
	TestTrue(TEXT("sequence session starts"), Sequence.BeginConnection(SequenceGeneration));
	TestTrue(TEXT("sequence session handshake"),
			 Sequence.AcceptServerHighwater(SequenceGeneration, 0, Error));
	OUC::FWireFrame SequenceFrame;
	TestEqual(TEXT("sequence frame builds"),
			  OUC::BuildHelloFrame(TEXT("emitter"), 0, TEXT("client"), TEXT("sequence"), TEXT(""),
								   SequenceFrame),
			  openusdconnect::client::FrameResult::Success);
	TestTrue(TEXT("sequence transaction queued"),
			 Sequence.EnqueueFrame(SequenceGeneration, 1, MoveTemp(SequenceFrame)));
	Sequence.MarkRejected(SequenceGeneration, 1, 2, TEXT("unexpected transaction id"));
	TestEqual(TEXT("sequence contradiction is session-fatal"), Sequence.GetRecoveryDisposition(),
			  EUSDConnectRecoveryDisposition::SessionFatal);
	return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
