// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#if WITH_DEV_AUTOMATION_TESTS

#include "SyncClient.h"
#include "Misc/AutomationTest.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FOpenUSDConnectReceiverAppliedCursorTest,
								 "OpenUSDConnect.Receiver.AppliedCursorIsMonotonicAndResettable",
								 EAutomationTestFlags::EditorContext |
									 EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectReceiverAppliedCursorTest::RunTest(const FString& Parameters)
{
	FReceiverSession Session(42, 4);

	TestEqual(TEXT("reconnect starts from the last successfully applied sequence"),
			  Session.LastAppliedSequence(), 41);
	const openusdconnect::client::ConnectionStart Connection = Session.BeginConnection();
	TestEqual(TEXT("invalid replay metadata is rejected without throwing"),
			  Session.AcceptReplayComplete(Connection.Generation, -1, 0),
			  openusdconnect::client::AcceptResult::InvalidSequence);
	FValidatedReceiverFrame Frame;
	Frame.Bytes = {1};
	Frame.Sequence = 42;
	TestEqual(TEXT("the receive thread accepts the next ordered frame"),
			  Session.Accept(Connection.Generation,
							 openusdconnect::client::ReceiverMessageKind::Event, 42,
							 MoveTemp(Frame)),
			  openusdconnect::client::AcceptResult::Accepted);
	FValidatedReceiverFrame Drained;
	TestTrue(TEXT("the game thread pops one frame without allocating a batch"),
			 Session.TryPop(Drained));
	TestEqual(TEXT("the popped frame retains its sequence metadata"), Drained.Sequence, 42);
	TestTrue(TEXT("successful game-thread application advances the cursor"),
			 Session.MarkAppliedThrough(Connection.Generation, 42));
	TestEqual(TEXT("successful game-thread application advances the cursor"),
			  Session.LastAppliedSequence(), 42);
	TestFalse(TEXT("an older observation is rejected"),
			  Session.MarkAppliedThrough(Connection.Generation, 40));
	TestEqual(TEXT("an older observation cannot move the cursor backward"),
			  Session.LastAppliedSequence(), 42);
	Session.ResetAppliedProgress();
	TestEqual(TEXT("an explicit server resync resets the applied cursor"),
			  Session.LastAppliedSequence(), 0);
	return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
