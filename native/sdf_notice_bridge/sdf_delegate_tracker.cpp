// SdfLayerStateDelegate-based change tracker.  Intercepts every Sdf mutation
// before it happens so the old value is still readable from the layer.  No
// layer snapshots, ExportToString diffs, or ChangeList polling are needed.

#include "sdf_delegate_bridge.h"

#include <pxr/pxr.h>
#include <pxr/base/tf/weakPtr.h>
#include <pxr/base/vt/value.h>
#include <pxr/usd/sdf/layer.h>
#include <pxr/usd/sdf/layerStateDelegate.h>
#include <pxr/usd/sdf/path.h>

#include <algorithm>
#include <deque>
#include <mutex>
#include <string>
#include <vector>

PXR_NAMESPACE_USING_DIRECTIVE

namespace ouc {
namespace {

std::string ValueToString(const VtValue &v) {
    if (v.IsEmpty())                 return "null";
    if (v.IsHolding<bool>())         return v.UncheckedGet<bool>() ? "true" : "false";
    if (v.IsHolding<int>())          return std::to_string(v.UncheckedGet<int>());
    if (v.IsHolding<int64_t>())      return std::to_string(v.UncheckedGet<int64_t>());
    if (v.IsHolding<unsigned>())     return std::to_string(v.UncheckedGet<unsigned>());
    if (v.IsHolding<double>())       return std::to_string(v.UncheckedGet<double>());
    if (v.IsHolding<float>())        return std::to_string(v.UncheckedGet<float>());
    if (v.IsHolding<std::string>())  return '"' + v.UncheckedGet<std::string>() + '"';
    if (v.IsHolding<TfToken>())      return '"' + v.UncheckedGet<TfToken>().GetString() + '"';
    if (v.IsHolding<SdfPath>())      return '"' + v.UncheckedGet<SdfPath>().GetString() + '"';
    return '"' + v.GetTypeName() + '"';
}

struct Record {
    std::string layerId, path, field, oldJson, newJson;
    bool hasOld = false;
};

class Batch {
    std::vector<Record> items_;
    std::vector<OucSdfDelegateRecord> views_;
    size_t byteSize_ = 0;
public:
    void push(Record r) {
        byteSize_ += r.layerId.size() + r.path.size() + r.field.size()
                   + r.oldJson.size() + r.newJson.size();
        items_.push_back(std::move(r));
    }
    bool   empty()     const noexcept { return items_.empty(); }
    size_t byteSize()  const noexcept { return byteSize_; }

    void finalise() {
        views_.clear(); views_.reserve(items_.size());
        for (const auto &r : items_) {
            views_.push_back({
                r.layerId.data(), r.layerId.size(),
                r.path.data(),    r.path.size(),
                r.field.data(),   r.field.size(),
                r.hasOld ? OUC_SDF_DELEGATE_FLAG_HAS_OLD_VALUE : UINT64_C(0),
                r.oldJson.empty() ? nullptr : r.oldJson.data(), r.oldJson.size(),
                r.newJson.empty() ? nullptr : r.newJson.data(), r.newJson.size(),
            });
        }
    }
    const OucSdfDelegateRecord *data() const noexcept { return views_.data(); }
    size_t                      size() const noexcept { return views_.size(); }
};

class Delegate final : public SdfLayerStateDelegateBase {
    std::unordered_map<std::string, SdfLayerStateDelegateBaseRefPtr> prev_;
    std::deque<Batch> batches_;
    std::mutex  mutex_;
    SdfLayerHandle layer_;
    bool    suppressed_  = false;
    bool    dirty_       = false;
    size_t  maxBytes_    = 0;
    uint64_t coalesced_  = 0;

public:
    explicit Delegate(size_t maxBytes) : maxBytes_(maxBytes) {}
    ~Delegate() override { restoreAll(); }

    void track(const char *const *ids, size_t count) {
        std::scoped_lock lock(mutex_);
        for (auto it = prev_.begin(); it != prev_.end(); ) {
            bool keep = std::any_of(ids, ids + count,
                [&](auto *s) { return s && it->first == s; });
            if (!keep) { restore(it->first); it = prev_.erase(it); }
            else ++it;
        }
        for (size_t i = 0; i < count; ++i) {
            if (!ids[i]) continue;
            std::string ident(ids[i]);
            if (prev_.count(ident)) continue;
            if (auto l = SdfLayer::Find(ident)) {
                prev_[ident] = l->GetStateDelegate();
                l->SetStateDelegate(TfCreateWeakPtr(
                    static_cast<SdfLayerStateDelegateBase *>(this)));
            }
        }
    }

    void setSuppressed(bool s) noexcept { suppressed_ = s; }

    const Batch *acquire() {
        std::scoped_lock lock(mutex_);
        return batches_.empty() ? nullptr : &batches_.front();
    }

    void release() {
        std::scoped_lock lock(mutex_);
        if (batches_.empty()) return;
        batches_.pop_front();
        coalesced_ += batches_.size();
    }

    size_t   pending()   const { return batches_.size(); }
    uint64_t coalesced() const noexcept { return coalesced_; }

private:
    void enqueue(Record r) {
        if (batches_.empty() ||
            (maxBytes_ && batches_.back().byteSize() > maxBytes_))
            batches_.emplace_back();
        batches_.back().push(std::move(r));
    }

    std::string layerId() const {
        return layer_ ? layer_->GetIdentifier() : std::string{};
    }

    bool captureOld(Record &r, const SdfPath &path, const TfToken &field) {
        if (!layer_ || !layer_->HasField(path, field))
            return true;  // new field, always record
        VtValue old = layer_->GetField(path, field);
        std::string oldStr = ValueToString(old);
        if (oldStr == r.newJson)
            return false; // unchanged, skip
        r.oldJson = std::move(oldStr);
        r.hasOld = true;
        return true;
    }

    void restore(const std::string &id) {
        if (auto l = SdfLayer::Find(id))
            l->SetStateDelegate(prev_[id]);
    }
    void restoreAll() {
        for (auto &[id, prevPtr] : prev_)
            if (auto l = SdfLayer::Find(id))
                l->SetStateDelegate(prevPtr);
        prev_.clear();
    }

    // ── SdfLayerStateDelegateBase overrides ─────────────────────────

    bool _IsDirty() override { return dirty_; }
    void _MarkCurrentStateAsClean() override { dirty_ = false; }
    void _MarkCurrentStateAsDirty() override { dirty_ = true; }
    void _OnSetLayer(const SdfLayerHandle &l) override { layer_ = l; }

    void _OnSetField(const SdfPath &path, const TfToken &field,
                     const VtValue &newVal) override {
        if (suppressed_) return;
        std::scoped_lock lock(mutex_);
        Record r{layerId(), path.GetString(), field.GetString(),
                 {}, ValueToString(newVal)};
        if (captureOld(r, path, field))
            enqueue(std::move(r));
    }

    void _OnSetField(const SdfPath &path, const TfToken &field,
                     const SdfAbstractDataConstValue &v) override {
        if (suppressed_) return;
        VtValue newVal; v.GetValue(&newVal);
        std::scoped_lock lock(mutex_);
        Record r{layerId(), path.GetString(), field.GetString(),
                 {}, ValueToString(newVal)};
        if (captureOld(r, path, field))
            enqueue(std::move(r));
    }

    void _OnCreateSpec(const SdfPath &path, SdfSpecType type, bool) override {
        if (suppressed_) return;
        std::scoped_lock lock(mutex_);
        enqueue({layerId(), path.GetString(), "_createSpec",
                 {}, '"' + TfEnum::GetDisplayName(type) + '"'});
    }

    void _OnDeleteSpec(const SdfPath &path, bool) override {
        if (suppressed_) return;
        std::scoped_lock lock(mutex_);
        enqueue({layerId(), path.GetString(), "_deleteSpec",
                 {}, "null", /*hasOld=*/true});
    }

    void _OnSetTimeSample(const SdfPath &path, double,
                          const VtValue &newVal) override {
        if (suppressed_) return;
        std::scoped_lock lock(mutex_);
        enqueue({layerId(), path.GetString(), "_setTimeSample",
                 {}, ValueToString(newVal), /*hasOld=*/true});
    }

    void _OnSetTimeSample(const SdfPath &path, double,
                          const SdfAbstractDataConstValue &v) override {
        if (suppressed_) return;
        VtValue newVal; v.GetValue(&newVal);
        std::scoped_lock lock(mutex_);
        enqueue({layerId(), path.GetString(), "_setTimeSample",
                 {}, ValueToString(newVal), /*hasOld=*/true});
    }

    void _OnMoveSpec(const SdfPath &oldPath, const SdfPath &newPath) override {
        if (suppressed_) return;
        std::scoped_lock lock(mutex_);
        std::string typeStr = "\"unknown\"";
        if (layer_) {
            if (auto spec = layer_->GetObjectAtPath(oldPath))
                typeStr = '"' + TfEnum::GetDisplayName(spec->GetSpecType()) + '"';
        }
        enqueue({layerId(), oldPath.GetString(), "_deleteSpec",
                 {}, "null", /*hasOld=*/true});
        enqueue({layerId(), newPath.GetString(), "_createSpec",
                 {}, typeStr});
    }

    // Stubs for remaining abstract methods.
    void _OnSetFieldDictValueByKey(const SdfPath &, const TfToken &,
                                   const TfToken &, const VtValue &) override {}
    void _OnSetFieldDictValueByKey(const SdfPath &, const TfToken &,
                                   const TfToken &,
                                   const SdfAbstractDataConstValue &) override {}
    void _OnPushChild(const SdfPath &, const TfToken &, const TfToken &) override {}
    void _OnPushChild(const SdfPath &, const TfToken &, const SdfPath &) override {}
    void _OnPopChild(const SdfPath &, const TfToken &, const TfToken &) override {}
    void _OnPopChild(const SdfPath &, const TfToken &, const SdfPath &) override {}
};

TF_REGISTRY_FUNCTION(TfType) {
    TfType::Define<Delegate, TfType::Bases<SdfLayerStateDelegateBase>>();
}

} // namespace
} // namespace ouc

extern "C" {

OucSdfDelegateTracker *
ouc_sdf_delegate_tracker_create(const char *const *ids, size_t n, size_t maxB)
{
    try {
        auto *d = new ouc::Delegate(maxB);
        if (n) d->track(ids, n);
        return reinterpret_cast<OucSdfDelegateTracker *>(d);
    } catch (...) { return nullptr; }
}

void ouc_sdf_delegate_tracker_destroy(OucSdfDelegateTracker *t)
{ delete reinterpret_cast<ouc::Delegate *>(t); }

int ouc_sdf_delegate_tracker_set_layers(OucSdfDelegateTracker *t,
    const char *const *ids, size_t n) {
    if (!t) return -1;
    reinterpret_cast<ouc::Delegate *>(t)->track(ids, n);
    return 0;
}

int ouc_sdf_delegate_tracker_set_suppressed(OucSdfDelegateTracker *t, int s) {
    if (!t) return -1;
    reinterpret_cast<ouc::Delegate *>(t)->setSuppressed(s != 0);
    return 0;
}

int ouc_sdf_delegate_tracker_acquire(OucSdfDelegateTracker *t,
    OucSdfDelegateBatch *out) {
    if (!t || !out) return -1;
    auto *b = reinterpret_cast<ouc::Delegate *>(t)->acquire();
    if (!b) return 0;
    const_cast<ouc::Batch *>(b)->finalise();
    out->records = b->data(); out->record_count = b->size();
    return 1;
}

int ouc_sdf_delegate_tracker_release(OucSdfDelegateTracker *t) {
    if (!t) return -1;
    reinterpret_cast<ouc::Delegate *>(t)->release();
    return 0;
}

size_t ouc_sdf_delegate_tracker_pending_batches(const OucSdfDelegateTracker *t)
{ return t ? reinterpret_cast<const ouc::Delegate *>(t)->pending() : 0; }

uint64_t ouc_sdf_delegate_tracker_coalesced_batch_count(
    const OucSdfDelegateTracker *t)
{ return t ? reinterpret_cast<const ouc::Delegate *>(t)->coalesced() : 0; }

} // extern "C"
