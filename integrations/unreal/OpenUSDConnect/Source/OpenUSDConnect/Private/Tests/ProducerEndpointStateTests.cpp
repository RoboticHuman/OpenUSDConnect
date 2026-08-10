// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#if WITH_DEV_AUTOMATION_TESTS

#include "EmitClient.h"
#include "Misc/AutomationTest.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FOpenUSDConnectProducerOutboxReconnectTest,
	"OpenUSDConnect.Producer.OutboxSurvivesClientReplacement",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerOutboxReconnectTest::RunTest(const FString& Parameters)
{
	TSharedRef<FProducerEndpointState> State = MakeShared<FProducerEndpointState>(
		TEXT("127.0.0.1"), 7200, TEXT(""), TEXT("session-a"));
	TestTrue(TEXT("same endpoint matches"), State->MatchesEndpoint(TEXT("127.0.0.1"), 7200, TEXT("")));
	TestFalse(TEXT("different port is a different producer endpoint"),
		State->MatchesEndpoint(TEXT("127.0.0.1"), 7201, TEXT("")));

	FQueuedProducerTxn FirstClaim;
	{
		FEmitClient FirstClient(nullptr, TEXT("client-a"), State, 0.01f);
		TArray<uint8> Frame = { 1, 2, 3, 4 };
		TestTrue(TEXT("transaction one is accepted"),
			FirstClient.EnqueueFrame(1, MoveTemp(Frame)));
		TestEqual(TEXT("next transaction advances"), State->GetNextTransactionId(), uint64(2));
		TestEqual(TEXT("one transaction remains pending"),
			State->GetPendingTransactionCount(), uint64(1));
		TestTrue(TEXT("first client claims the frame"), State->ClaimNextUnsent(FirstClaim));
		TestEqual(TEXT("claimed transaction identity"), FirstClaim.TxnId, uint64(1));
		TestTrue(TEXT("claimed encoded frame remains owned by endpoint state"),
			FirstClaim.Frame.IsValid());
	}

	// The subsystem marks the endpoint outbox unsent when one socket client
	// stops. A replacement object then claims the same identity and exact bytes.
	State->MarkAllUnsent();
	FQueuedProducerTxn ReplacementClaim;
	{
		FEmitClient ReplacementClient(nullptr, TEXT("client-a"), State, 0.01f);
		TestTrue(TEXT("replacement client reclaims the pending frame"),
			State->ClaimNextUnsent(ReplacementClaim));
		TestFalse(TEXT("zero-timeout flush reports outstanding durability"),
			ReplacementClient.FlushPending(0.0));
	}
	TestEqual(TEXT("replacement keeps transaction identity"), ReplacementClaim.TxnId, uint64(1));
	TestTrue(TEXT("replacement keeps exact encoded bytes"),
		ReplacementClaim.Frame == FirstClaim.Frame);

	State->RetireThrough(1);
	TestEqual(TEXT("acknowledgement empties the outbox"), State->GetPendingTransactionCount(), uint64(0));
	TestEqual(TEXT("acknowledgement counter advances"),
		State->GetAcknowledgedTransactionCount(), uint64(1));
	FEmitClient AcknowledgedClient(nullptr, TEXT("client-a"), State, 0.01f);
	TestTrue(TEXT("flush succeeds once the endpoint outbox is acknowledged"),
		AcknowledgedClient.FlushPending(0.0));
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FOpenUSDConnectProducerEndpointIsolationTest,
	"OpenUSDConnect.Producer.EndpointIsolationAndHighwater",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerEndpointIsolationTest::RunTest(const FString& Parameters)
{
	FProducerEndpointState First(TEXT("server-a"), 7200, TEXT(""), TEXT("session-a"));
	TArray<uint8> Frame = { 9 };
	TestTrue(TEXT("first endpoint accepts transaction one"), First.EnqueueFrame(1, MoveTemp(Frame)));
	FString Error;
	TestTrue(TEXT("matching durable highwater is accepted"), First.AcceptServerHighwater(1, Error));
	TestFalse(TEXT("durable highwater regression is rejected"), First.AcceptServerHighwater(0, Error));
	TestTrue(TEXT("regression requires explicit recovery"), First.IsRecoveryRequired());

	FProducerEndpointState Second(TEXT("server-b"), 7200, TEXT(""), TEXT("session-b"));
	TestEqual(TEXT("new endpoint starts a new ordered session"),
		Second.GetNextTransactionId(), uint64(1));
	TestFalse(TEXT("new endpoint does not inherit recovery state"), Second.IsRecoveryRequired());
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FOpenUSDConnectProducerRejectionDispositionTest,
	"OpenUSDConnect.Producer.RejectionDisposition",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectProducerRejectionDispositionTest::RunTest(const FString& Parameters)
{
	FProducerEndpointState Conflict(TEXT("server"), 7200, TEXT(""), TEXT("conflict"));
	Conflict.MarkRejected(1, 3, TEXT("obsolete layer graph"));
	TestEqual(TEXT("stale graph is recoverable"),
		Conflict.GetRecoveryDisposition(),
		EUSDConnectRecoveryDisposition::RecoverableConflict);

	FProducerEndpointState Invalid(TEXT("server"), 7200, TEXT(""), TEXT("invalid"));
	Invalid.MarkRejected(1, 4, TEXT("invalid event"));
	TestEqual(TEXT("malformed operation is an integration fault"),
		Invalid.GetRecoveryDisposition(),
		EUSDConnectRecoveryDisposition::InvalidOperation);

	FProducerEndpointState Sequence(TEXT("server"), 7200, TEXT(""), TEXT("sequence"));
	Sequence.MarkRejected(1, 2, TEXT("unexpected transaction id"));
	TestEqual(TEXT("sequence contradiction is session-fatal"),
		Sequence.GetRecoveryDisposition(),
		EUSDConnectRecoveryDisposition::SessionFatal);
	return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
