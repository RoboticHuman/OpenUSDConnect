#ifndef OPENUSDCONNECT_SDF_DELEGATE_BRIDGE_H
#define OPENUSDCONNECT_SDF_DELEGATE_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(OUC_SDF_DELEGATE_BRIDGE_BUILD)
#define OUC_SDF_DELEGATE_API __declspec(dllexport)
#else
#define OUC_SDF_DELEGATE_API __declspec(dllimport)
#endif
#elif defined(__GNUC__) || defined(__clang__)
#define OUC_SDF_DELEGATE_API __attribute__((visibility("default")))
#else
#define OUC_SDF_DELEGATE_API
#endif

#ifdef __cplusplus
extern "C"
{
#endif

#define OUC_SDF_DELEGATE_ABI_VERSION UINT32_C(1)

	// ---- Delegate-based tracker (SdfLayerStateDelegate) ----

	typedef struct OucSdfDelegateTracker OucSdfDelegateTracker;

#define OUC_SDF_DELEGATE_FLAG_HAS_OLD_VALUE UINT64_C(1)

	typedef struct OucSdfDelegateRecord
	{
		const char* layer_identifier;
		size_t layer_identifier_size;
		const char* path;
		size_t path_size;
		const char* field_name;
		size_t field_name_size;
		uint64_t flags;
		const char* old_value_json;
		size_t old_value_json_size;
		const char* new_value_json;
		size_t new_value_json_size;
	} OucSdfDelegateRecord;

	typedef struct OucSdfDelegateBatch
	{
		const OucSdfDelegateRecord* records;
		size_t record_count;
	} OucSdfDelegateBatch;

	OUC_SDF_DELEGATE_API OucSdfDelegateTracker*
	ouc_sdf_delegate_tracker_create(const char* const* layer_identifiers, size_t layer_count,
									size_t max_queued_bytes);

	OUC_SDF_DELEGATE_API void ouc_sdf_delegate_tracker_destroy(OucSdfDelegateTracker* tracker);

	OUC_SDF_DELEGATE_API int
	ouc_sdf_delegate_tracker_set_layers(OucSdfDelegateTracker* tracker,
										const char* const* layer_identifiers, size_t layer_count);

	OUC_SDF_DELEGATE_API int ouc_sdf_delegate_tracker_set_suppressed(OucSdfDelegateTracker* tracker,
																	 int suppressed);

	OUC_SDF_DELEGATE_API int ouc_sdf_delegate_tracker_acquire(OucSdfDelegateTracker* tracker,
															  OucSdfDelegateBatch* batch);

	OUC_SDF_DELEGATE_API int ouc_sdf_delegate_tracker_release(OucSdfDelegateTracker* tracker);

	OUC_SDF_DELEGATE_API size_t
	ouc_sdf_delegate_tracker_pending_batches(const OucSdfDelegateTracker* tracker);

	OUC_SDF_DELEGATE_API uint64_t
	ouc_sdf_delegate_tracker_coalesced_batch_count(const OucSdfDelegateTracker* tracker);

	// ---- Shared utilities ----

	OUC_SDF_DELEGATE_API const char* ouc_sdf_delegate_last_error(void);

	OUC_SDF_DELEGATE_API uint32_t ouc_sdf_delegate_pxr_version(void);

	OUC_SDF_DELEGATE_API uint32_t ouc_sdf_delegate_abi_version(void);

#ifdef __cplusplus
}
#endif

#endif
