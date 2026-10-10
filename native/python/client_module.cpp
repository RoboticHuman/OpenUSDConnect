#include "driver_bindings.h"

#include "openusdconnect/client/engine/status.h"
#include "openusdconnect/client/producer_recovery.h"
#include "openusdconnect/client/producer_session.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string_view.h>

namespace nb = nanobind;
using namespace nb::literals;
using openusdconnect::client::ClientPhase;
using openusdconnect::client::PhaseInputs;
using openusdconnect::client::ProducerRecoveryDisposition;
using openusdconnect::client::ProducerResult;

// Defined in receiver_bindings.cpp and producer_bindings.cpp.
void BindReceiver(nb::module_& module);
void BindProducer(nb::module_& module);

NB_MODULE(_native_client, module)
{
	module.doc() = "Native OpenUSDConnect client engine";

	nb::enum_<ProducerResult>(module, "ProducerResult")
		.value("ACCEPTED", ProducerResult::Accepted)
		.value("STALE_GENERATION", ProducerResult::StaleGeneration)
		.value("INVALID_PHASE", ProducerResult::InvalidPhase)
		.value("RECOVERY_REQUIRED", ProducerResult::RecoveryRequired)
		.value("HIGHWATER_AHEAD", ProducerResult::HighwaterAhead)
		.value("HIGHWATER_REGRESSED", ProducerResult::HighwaterRegressed)
		.value("OUTBOX_FULL", ProducerResult::OutboxFull)
		.value("SEQUENCE_MISMATCH", ProducerResult::SequenceMismatch)
		.value("NO_PENDING_TRANSACTION", ProducerResult::NoPendingTransaction)
		.value("TRANSACTION_MISSING", ProducerResult::TransactionMissing)
		.value("RECOVERY_NOT_RECOVERABLE", ProducerResult::RecoveryNotRecoverable)
		.value("INVALID_ARGUMENT", ProducerResult::InvalidArgument);

	nb::enum_<ProducerRecoveryDisposition>(module, "ProducerRecoveryDisposition")
		.value("NONE", ProducerRecoveryDisposition::None)
		.value("RECOVERABLE_CONFLICT", ProducerRecoveryDisposition::RecoverableConflict)
		.value("INVALID_OPERATION", ProducerRecoveryDisposition::InvalidOperation)
		.value("SESSION_FATAL", ProducerRecoveryDisposition::SessionFatal);

	module.def("rejection_code_name", &openusdconnect::client::RejectionCodeName, "code"_a);
	module.def("rejection_disposition", &openusdconnect::client::RejectionDisposition, "code"_a);

	nb::enum_<ClientPhase>(module, "ClientPhase")
		.value("OFFLINE", ClientPhase::Offline)
		.value("CONNECTING", ClientPhase::Connecting)
		.value("REPLAYING", ClientPhase::Replaying)
		.value("READY", ClientPhase::Ready)
		.value("RECOVERY_REQUIRED", ClientPhase::RecoveryRequired)
		.value("REJECTED", ClientPhase::Rejected)
		.value("CLOSED", ClientPhase::Closed)
		.value("PARKED", ClientPhase::Parked);

	module.def(
		"compute_phase",
		[](bool closed, bool recovery_required, bool rejected, bool parked, bool replaying,
		   bool ready, bool connecting)
		{
			return openusdconnect::client::ComputePhase(
				{closed, recovery_required, rejected, parked, replaying, ready, connecting});
		},
		nb::kw_only(), "closed"_a, "recovery_required"_a, "rejected"_a, "parked"_a, "replaying"_a,
		"ready"_a, "connecting"_a);

	openusdconnect::python::BindDriverTypes(module);
	BindReceiver(module);
	BindProducer(module);
}
