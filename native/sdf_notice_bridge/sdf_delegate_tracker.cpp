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
#include <atomic>
#include <deque>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
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
    const std::vector<Record> &items() const noexcept { return items_; }

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

class Tracker;

class LayerDelegate final : public SdfLayerStateDelegateBase {
    Tracker *owner_ = nullptr;
    SdfLayerHandle layer_;
    bool dirty_ = false;

public:
    explicit LayerDelegate(Tracker *owner) : owner_(owner) {}
    ~LayerDelegate() override = default;

private:
    std::string layerId() const;
    bool captureOld(Record &r, const SdfPath &path, const TfToken &field) const;
    void enqueueField(const SdfPath &path, const TfToken &field);

    bool _IsDirty() override { return dirty_; }
    void _MarkCurrentStateAsClean() override { dirty_ = false; }
    void _MarkCurrentStateAsDirty() override { dirty_ = true; }
    void _OnSetLayer(const SdfLayerHandle &layer) override { layer_ = layer; }

    void _OnSetField(const SdfPath &, const TfToken &, const VtValue &) override;
    void _OnSetField(const SdfPath &, const TfToken &,
                     const SdfAbstractDataConstValue &) override;
    void _OnSetFieldDictValueByKey(const SdfPath &, const TfToken &,
                                   const TfToken &, const VtValue &) override;
    void _OnSetFieldDictValueByKey(const SdfPath &, const TfToken &,
                                   const TfToken &,
                                   const SdfAbstractDataConstValue &) override;
    void _OnCreateSpec(const SdfPath &, SdfSpecType, bool) override;
    void _OnDeleteSpec(const SdfPath &, bool) override;
    void _OnSetTimeSample(const SdfPath &, double, const VtValue &) override;
    void _OnSetTimeSample(const SdfPath &, double,
                          const SdfAbstractDataConstValue &) override;
    void _OnMoveSpec(const SdfPath &, const SdfPath &) override;
    void _OnPushChild(const SdfPath &, const TfToken &, const TfToken &) override;
    void _OnPushChild(const SdfPath &, const TfToken &, const SdfPath &) override;
    void _OnPopChild(const SdfPath &, const TfToken &, const TfToken &) override;
    void _OnPopChild(const SdfPath &, const TfToken &, const SdfPath &) override;
};

class Tracker final {
    struct InstalledDelegate {
        SdfLayerStateDelegateBaseRefPtr previous;
        SdfLayerStateDelegateBaseRefPtr tracker;
    };

    std::unordered_map<std::string, InstalledDelegate> delegates_;
    std::deque<Batch> batches_;
    std::unordered_set<std::string> overflowLayers_;
    mutable std::mutex mutex_;
    std::atomic_bool suppressed_{false};
    size_t maxBytes_ = 0;
    size_t queuedBytes_ = 0;
    uint64_t coalesced_ = 0;

public:
    explicit Tracker(size_t maxBytes) : maxBytes_(maxBytes) {}
    ~Tracker() { restoreAll(); }

    void track(const char *const *ids, size_t count) {
        std::scoped_lock lock(mutex_);
        std::unordered_set<std::string> desired;
        for (size_t i = 0; i < count; ++i)
            if (ids[i]) desired.emplace(ids[i]);
        for (auto it = delegates_.begin(); it != delegates_.end(); ) {
            const bool keep = desired.count(it->first) != 0;
            if (!keep) { restore(it->first, it->second); it = delegates_.erase(it); }
            else ++it;
        }
        for (size_t i = 0; i < count; ++i) {
            if (!ids[i]) continue;
            std::string ident(ids[i]);
            if (auto existing = delegates_.find(ident); existing != delegates_.end()) {
                if (auto layer = SdfLayer::Find(ident))
                    layer->SetStateDelegate(existing->second.tracker);
                continue;
            }
            if (auto l = SdfLayer::Find(ident)) {
                InstalledDelegate installed;
                installed.previous = l->GetStateDelegate();
                installed.tracker = TfCreateRefPtr(new LayerDelegate(this));
                l->SetStateDelegate(installed.tracker);
                delegates_.emplace(ident, std::move(installed));
            }
        }
    }

    void setSuppressed(bool s) noexcept { suppressed_.store(s); }
    bool suppressed() const noexcept { return suppressed_.load(); }

    const Batch *acquire() {
        std::scoped_lock lock(mutex_);
        return batches_.empty() ? nullptr : &batches_.front();
    }

    void release() {
        std::scoped_lock lock(mutex_);
        if (batches_.empty()) return;
        queuedBytes_ -= batches_.front().byteSize();
        batches_.pop_front();
        if (batches_.empty()) overflowLayers_.clear();
    }

    size_t pending() const {
        std::scoped_lock lock(mutex_);
        return batches_.size();
    }
    uint64_t coalesced() const {
        std::scoped_lock lock(mutex_);
        return coalesced_;
    }

    void enqueue(Record r) {
        std::scoped_lock lock(mutex_);
        const size_t recordBytes = r.layerId.size() + r.path.size() + r.field.size()
                                 + r.oldJson.size() + r.newJson.size();

        if (!overflowLayers_.empty()) {
            addOverflowMarker(r.layerId);
            return;
        }
        if (maxBytes_ && queuedBytes_ + recordBytes > maxBytes_) {
            std::unordered_set<std::string> affected;
            for (const auto &batch : batches_)
                for (const auto &item : batch.items())
                    affected.insert(item.layerId);
            affected.insert(r.layerId);
            batches_.clear();
            queuedBytes_ = 0;
            overflowLayers_.clear();
            ++coalesced_;
            std::vector<std::string> ordered(affected.begin(), affected.end());
            std::sort(ordered.begin(), ordered.end());
            for (const auto &layerId : ordered) addOverflowMarker(layerId);
            return;
        }
        if (batches_.empty()) batches_.emplace_back();
        queuedBytes_ += recordBytes;
        batches_.back().push(std::move(r));
    }

private:
    void addOverflowMarker(const std::string &layerId) {
        if (!overflowLayers_.insert(layerId).second) return;
        Record marker{layerId, "/", "_replaceLayerContent", {}, "null"};
        const size_t bytes = marker.layerId.size() + marker.path.size()
                           + marker.field.size() + marker.newJson.size();
        if (batches_.empty()) batches_.emplace_back();
        queuedBytes_ += bytes;
        batches_.back().push(std::move(marker));
    }

    void restore(const std::string &id, const InstalledDelegate &installed) {
        if (auto layer = SdfLayer::Find(id))
            layer->SetStateDelegate(installed.previous);
    }

    void restoreAll() {
        for (const auto &[id, installed] : delegates_)
            restore(id, installed);
        delegates_.clear();
    }
};

std::string LayerDelegate::layerId() const {
    return layer_ ? layer_->GetIdentifier() : std::string{};
}

bool LayerDelegate::captureOld(
    Record &r, const SdfPath &path, const TfToken &field) const {
    if (!layer_ || !layer_->HasField(path, field))
        return true;
    VtValue old = layer_->GetField(path, field);
    std::string oldStr = ValueToString(old);
    r.oldJson = std::move(oldStr);
    r.hasOld = true;
    return true;
}

void LayerDelegate::enqueueField(const SdfPath &path, const TfToken &field) {
    if (owner_->suppressed()) return;
    owner_->enqueue({layerId(), path.GetString(), field.GetString(), {}, "null"});
}

void LayerDelegate::_OnSetField(
    const SdfPath &path, const TfToken &field, const VtValue &newVal) {
    if (owner_->suppressed()) return;
    Record r{layerId(), path.GetString(), field.GetString(), {}, ValueToString(newVal)};
    if (captureOld(r, path, field)) owner_->enqueue(std::move(r));
}

void LayerDelegate::_OnSetField(
    const SdfPath &path, const TfToken &field, const SdfAbstractDataConstValue &value) {
    VtValue newVal;
    value.GetValue(&newVal);
    _OnSetField(path, field, newVal);
}

void LayerDelegate::_OnSetFieldDictValueByKey(
    const SdfPath &path, const TfToken &field, const TfToken &, const VtValue &) {
    enqueueField(path, field);
}

void LayerDelegate::_OnSetFieldDictValueByKey(
    const SdfPath &path, const TfToken &field, const TfToken &,
    const SdfAbstractDataConstValue &) {
    enqueueField(path, field);
}

void LayerDelegate::_OnCreateSpec(const SdfPath &path, SdfSpecType type, bool) {
    if (owner_->suppressed()) return;
    owner_->enqueue({layerId(), path.GetString(), "_createSpec",
                     {}, '"' + TfEnum::GetDisplayName(type) + '"'});
}

void LayerDelegate::_OnDeleteSpec(const SdfPath &path, bool) {
    if (owner_->suppressed()) return;
    owner_->enqueue({layerId(), path.GetString(), "_deleteSpec",
                     {}, "null", /*hasOld=*/true});
}

void LayerDelegate::_OnSetTimeSample(
    const SdfPath &path, double, const VtValue &newVal) {
    if (owner_->suppressed()) return;
    owner_->enqueue({layerId(), path.GetString(), "_setTimeSample",
                     {}, ValueToString(newVal), /*hasOld=*/true});
}

void LayerDelegate::_OnSetTimeSample(
    const SdfPath &path, double time, const SdfAbstractDataConstValue &value) {
    VtValue newVal;
    value.GetValue(&newVal);
    _OnSetTimeSample(path, time, newVal);
}

void LayerDelegate::_OnMoveSpec(const SdfPath &oldPath, const SdfPath &newPath) {
    if (owner_->suppressed()) return;
    std::string typeStr = "\"unknown\"";
    if (layer_) {
        if (auto spec = layer_->GetObjectAtPath(oldPath))
            typeStr = '"' + TfEnum::GetDisplayName(spec->GetSpecType()) + '"';
    }
    owner_->enqueue({layerId(), oldPath.GetString(), "_deleteSpec",
                     {}, "null", /*hasOld=*/true});
    owner_->enqueue({layerId(), newPath.GetString(), "_createSpec", {}, typeStr});
}

void LayerDelegate::_OnPushChild(
    const SdfPath &, const TfToken &, const TfToken &) {}

void LayerDelegate::_OnPushChild(
    const SdfPath &, const TfToken &, const SdfPath &) {}

void LayerDelegate::_OnPopChild(
    const SdfPath &, const TfToken &, const TfToken &) {}

void LayerDelegate::_OnPopChild(
    const SdfPath &, const TfToken &, const SdfPath &) {}

} // namespace
} // namespace ouc

extern "C" {

OucSdfDelegateTracker *
ouc_sdf_delegate_tracker_create(const char *const *ids, size_t n, size_t maxB)
{
    try {
        auto *d = new ouc::Tracker(maxB);
        if (n) d->track(ids, n);
        return reinterpret_cast<OucSdfDelegateTracker *>(d);
    } catch (...) { return nullptr; }
}

void ouc_sdf_delegate_tracker_destroy(OucSdfDelegateTracker *t)
{ delete reinterpret_cast<ouc::Tracker *>(t); }

int ouc_sdf_delegate_tracker_set_layers(OucSdfDelegateTracker *t,
    const char *const *ids, size_t n) {
    if (!t) return -1;
    reinterpret_cast<ouc::Tracker *>(t)->track(ids, n);
    return 0;
}

int ouc_sdf_delegate_tracker_set_suppressed(OucSdfDelegateTracker *t, int s) {
    if (!t) return -1;
    reinterpret_cast<ouc::Tracker *>(t)->setSuppressed(s != 0);
    return 0;
}

int ouc_sdf_delegate_tracker_acquire(OucSdfDelegateTracker *t,
    OucSdfDelegateBatch *out) {
    if (!t || !out) return -1;
    auto *b = reinterpret_cast<ouc::Tracker *>(t)->acquire();
    if (!b) return 0;
    const_cast<ouc::Batch *>(b)->finalise();
    out->records = b->data(); out->record_count = b->size();
    return 1;
}

int ouc_sdf_delegate_tracker_release(OucSdfDelegateTracker *t) {
    if (!t) return -1;
    reinterpret_cast<ouc::Tracker *>(t)->release();
    return 0;
}

size_t ouc_sdf_delegate_tracker_pending_batches(const OucSdfDelegateTracker *t)
{ return t ? reinterpret_cast<const ouc::Tracker *>(t)->pending() : 0; }

uint64_t ouc_sdf_delegate_tracker_coalesced_batch_count(
    const OucSdfDelegateTracker *t)
{ return t ? reinterpret_cast<const ouc::Tracker *>(t)->coalesced() : 0; }

} // extern "C"
