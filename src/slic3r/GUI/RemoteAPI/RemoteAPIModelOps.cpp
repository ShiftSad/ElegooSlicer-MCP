// src/slic3r/GUI/RemoteAPI/RemoteAPIModelOps.cpp
//
// elegoo-mcp: model operations ElegooSlicer MCP adds to the Remote API.
// Each handler runs its GUI work inside run_on_ui and repeats the steps of the
// matching GUI action, leaving out that action's dialogs: an API call must never
// open a modal window, because nobody may be at the machine to close it.
#include "RemoteAPIController.hpp"

#include "libslic3r/Format/bbs_3mf.hpp"   // save_object_mesh (auto-backup)
#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleMesh.hpp"
#include "libslic3r/TriangleSelector.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/GLCanvas3D.hpp"
#include "slic3r/GUI/GUI_ObjectList.hpp"
#include "slic3r/GUI/I18N.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/ObjectDataViewModel.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/Selection.hpp"

#include <algorithm>
#include <cmath>

#include <boost/algorithm/string/predicate.hpp>
#include <boost/filesystem.hpp>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// --- POST /orient {"object_id": id} ------------------------------------------
// The window auto-orients the 3D selection (OrientJob::prepare_selected), so this
// selects the object as a click on it does and starts the same orient job.
Response Controller::handle_orient_object(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (!in.contains("object_id"))
        return handle_orient(); // "{}" means every object, like no body
    if (!in["object_id"].is_number_unsigned())
        return { 400, {{"error", "bad_object_id"}} };
    const uint64_t id = in["object_id"].get<uint64_t>();

    nlohmann::json r = run_on_ui([id]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        if (!plater->get_ui_job_worker().is_idle()) return {{"error", "job_running"}};
        // The job orients printable objects only, and when the selection holds nothing
        // it can orient it orients every OTHER object instead
        // (OrientJob::prepare_selection). So an unprintable object, or a selection that
        // did not take, must not start it.
        if (!plater->model().objects[idx]->printable) return {{"error", "object_not_printable"}};

        // Select only this object, the way Plater::priv::load_files selects new objects,
        // and let the object list follow the 3D scene.
        GLCanvas3D *canvas = plater->get_view3D_canvas3D();
        canvas->get_selection().add_object((unsigned int) idx, /*as_single_selection=*/true);
        if (canvas->get_selection().get_object_idx() != idx) return {{"error", "select_failed"}};
        wxGetApp().obj_list()->update_selections();
        if (canvas->get_gizmos_manager().is_enabled())
            canvas->update_gizmos_on_off_state();

        // Same reset as handle_orient: Elegoo's sticky "outside bed" prepare state makes
        // the job skip the selection.
        plater->set_prepare_state(Job::PREPARE_STATE_DEFAULT);
        // The job's finish (OrientJob::finalize -> Plater::update) re-checks only the
        // current plate, so mark the object's plates now, like every per-object edit
        // (see invalidate_object_plates). Harmless if the job ends up turning nothing.
        invalidate_object_plates(idx);
        plater->orient();
        return {{"started", true}, {"object_id", id}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object") return { 404, r };
        return { 409, r }; // job_running, object_not_printable, select_failed
    }
    return { 202, r };
}

// --- POST /view {"view": "prepare" | "preview"} ------------------------------
// Which main-window tab is showing: "prepare" (3D editor), "preview" or "other"
// (home, device, ... pages).
std::string main_view_name()
{
    const int tab = wxGetApp().mainframe->current_tab();
    if (tab == MainFrame::tp3DEditor) return "prepare";
    if (tab == MainFrame::tpPreview)  return "preview";
    return "other";
}

// Switches the main window to the Prepare or Preview tab with the call the GUI uses
// for this (MainFrame::select_tab).
Response Controller::handle_set_view(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object() || !in.contains("view") || !in["view"].is_string())
        return { 400, {{"error", "missing_view"}, {"detail", "body {\"view\": \"prepare\" | \"preview\"}"}} };
    const std::string view = in["view"].get<std::string>();
    if (view != "prepare" && view != "preview")
        return { 422, {{"error", "bad_view"}, {"detail", "prepare | preview"}} };

    nlohmann::json r = run_on_ui([view]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        // With a G-code file open the window is preview-only, and its Prepare button
        // asks whether to close that file (MainFrame::preview_only_hint). An API call
        // cannot answer a question, so refuse instead.
        if (view == "prepare" && (plater->only_gcode_mode() || plater->using_exported_file()))
            return {{"error", "preview_only"}};
        wxGetApp().mainframe->select_tab(size_t(view == "prepare" ? MainFrame::tp3DEditor : MainFrame::tpPreview));
        return {{"view", main_view_name()}};
    });
    if (r.contains("error")) return { 409, r };
    return { 200, r };
}

// --- part volumes: /objects/{id}/volumes -------------------------------------
// The parts, modifiers, negative volumes and support enforcers/blockers listed under
// an object in the object list. Ids are ModelVolume ids, like the object ids.

static const char *volume_type_name(ModelVolumeType type)
{
    switch (type) {
    case ModelVolumeType::MODEL_PART:         return "part";
    case ModelVolumeType::PARAMETER_MODIFIER: return "modifier";
    case ModelVolumeType::NEGATIVE_VOLUME:    return "negative";
    case ModelVolumeType::SUPPORT_ENFORCER:   return "support_enforcer";
    case ModelVolumeType::SUPPORT_BLOCKER:    return "support_blocker";
    default:                                  return "other";
    }
}

static int find_volume_index(const ModelObject &mo, uint64_t vid)
{
    for (size_t i = 0; i < mo.volumes.size(); ++i)
        if ((uint64_t) mo.volumes[i]->id().id == vid)
            return (int) i;
    return -1;
}

// One volume as JSON. Size and position are in world mm, measured on the object's
// first copy: the same frame POST /objects/{id}/volumes takes them in.
static nlohmann::json volume_json(const ModelObject &mo, size_t index)
{
    const ModelVolume *mv = mo.volumes[index];
    Transform3d world = mv->get_matrix();
    if (!mo.instances.empty())
        world = mo.instances.front()->get_matrix() * world;
    const BoundingBoxf3 bb     = mv->mesh().transformed_bounding_box(world);
    const Vec3d         size   = bb.size();
    const Vec3d         center = bb.center();

    const DynamicPrintConfig overrides = mv->config.get();
    nlohmann::json cfg = nlohmann::json::object();
    for (const std::string &key : overrides.keys())
        cfg[key] = overrides.opt_serialize(key);
    return {
        {"id", (uint64_t) mv->id().id},
        {"index", index},
        {"name", mv->name},
        {"type", volume_type_name(mv->type())},
        {"extruder", mv->config.has("extruder") ? mv->config.extruder() : 0}, // 0 = inherits
        {"size_mm", {size.x(), size.y(), size.z()}},
        {"position_mm", {center.x(), center.y(), center.z()}},
        {"config", cfg},
    };
}

// Reads a [x, y, z] array of finite numbers.
static bool read_vec3(const nlohmann::json &value, Vec3d &out)
{
    if (!value.is_array() || value.size() != 3)
        return false;
    for (int i = 0; i < 3; ++i) {
        if (!value[i].is_number()) return false;
        out[i] = value[i].get<double>();
        if (!std::isfinite(out[i])) return false;
    }
    return true;
}

Response Controller::route_volumes(const Request &req, uint64_t id, const std::string &action)
{
    if (action == "volumes") {
        if (req.method == "GET")  return handle_get_volumes(id);
        if (req.method == "POST") return handle_add_volume(id, req.body);
        return { 404, {{"error", "not_found"}} };
    }
    // "volumes/<vid>" or "volumes/<vid>/config"
    const std::string rest   = action.substr(std::string("volumes/").size());
    const size_t      slash  = rest.find('/');
    const std::string vid_s  = rest.substr(0, slash);
    const std::string sub    = slash == std::string::npos ? std::string() : rest.substr(slash + 1);
    uint64_t vid = 0;
    try {
        size_t used = 0;
        vid = std::stoull(vid_s, &used);
        if (used != vid_s.size()) throw std::invalid_argument("trailing characters");
    } catch (...) {
        return { 400, {{"error", "bad_volume_id"}} };
    }
    if (req.method == "DELETE" && sub.empty())    return handle_delete_volume(id, vid);
    if (req.method == "PUT" && sub == "config")   return handle_put_volume_config(id, vid, req.body);
    return { 404, {{"error", "not_found"}} };
}

Response Controller::handle_get_volumes(uint64_t id)
{
    nlohmann::json r = run_on_ui([id]() -> nlohmann::json {
        const Model &model = wxGetApp().plater()->model();
        const int idx = find_object_index(model, id);
        if (idx < 0) return {{"error", "unknown_object"}};
        const ModelObject *mo = model.objects[idx];
        nlohmann::json volumes = nlohmann::json::array();
        for (size_t i = 0; i < mo->volumes.size(); ++i)
            volumes.push_back(volume_json(*mo, i));
        return {{"object", id}, {"volumes", volumes}};
    });
    if (r.contains("error")) return { 404, r };
    return { 200, r };
}

// POST /objects/{id}/volumes
//   {"type": "modifier|negative|support_enforcer|support_blocker",
//    "shape": "box|cylinder|sphere", "size_mm": [x,y,z], "position_mm": [x,y,z],
//    "name"?: "...", "config"?: {...}}
// Repeats ObjectList::load_generic_subobject (right-click > Add modifier > Cube...)
// without its "Add Modifier" tips dialog, with the size and position asked for
// instead of the menu's fixed size beside the object.
Response Controller::handle_add_volume(uint64_t id, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };

    const std::string type_s = in.contains("type") && in["type"].is_string() ? in["type"].get<std::string>() : "";
    ModelVolumeType type = type_s == "modifier"         ? ModelVolumeType::PARAMETER_MODIFIER :
                           type_s == "negative"         ? ModelVolumeType::NEGATIVE_VOLUME :
                           type_s == "support_enforcer" ? ModelVolumeType::SUPPORT_ENFORCER :
                           type_s == "support_blocker"  ? ModelVolumeType::SUPPORT_BLOCKER :
                                                          ModelVolumeType::INVALID;
    if (type == ModelVolumeType::INVALID)
        return { 422, {{"error", "bad_type"}, {"detail", "modifier | negative | support_enforcer | support_blocker"}} };

    const std::string shape = in.contains("shape") && in["shape"].is_string() ? in["shape"].get<std::string>() : "";
    if (shape != "box" && shape != "cylinder" && shape != "sphere")
        return { 422, {{"error", "bad_shape"}, {"detail", "box | cylinder | sphere"}} };

    Vec3d size, position;
    if (!in.contains("size_mm") || !read_vec3(in["size_mm"], size) || size.minCoeff() <= 0. || size.maxCoeff() > 10000.)
        return { 422, {{"error", "bad_size"}, {"detail", "size_mm: [x, y, z], each > 0 and <= 10000 mm"}} };
    if (!in.contains("position_mm") || !read_vec3(in["position_mm"], position))
        return { 422, {{"error", "bad_position"}, {"detail", "position_mm: [x, y, z], world mm of the centre"}} };
    if (in.contains("name") && !in["name"].is_string())
        return { 400, {{"error", "name_must_be_string"}} };
    if (in.contains("config") && !in["config"].is_object())
        return { 400, {{"error", "config_must_be_object"}} };
    const std::string    name      = in.value("name", std::string());
    const nlohmann::json config_in = in.value("config", nlohmann::json::object());

    nlohmann::json r = run_on_ui([id, type, type_s, shape, size, position, name, config_in]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        if (mo->instances.empty()) return {{"error", "no_instance"}};

        // Check the overrides before adding anything, so a refused key adds nothing.
        // A new modifier starts on "default" filament, as in the GUI.
        DynamicPrintConfig overrides;
        overrides.set_key_value("extruder", new ConfigOptionInt(0));
        nlohmann::json applied = nlohmann::json::array();
        nlohmann::json errors  = nlohmann::json::object();
        stage_object_config(overrides, config_in, applied, errors);
        if (!errors.empty()) {
            api_notify("Couldn't add a " + type_s + " to '" + mo->name + "'", true);
            return {{"error", "config_rejected"}, {"applied", nlohmann::json::array()}, {"errors", errors}};
        }

        plater->take_snapshot("Add primitive");

        // The menu's primitives (create_mesh in GUI_ObjectList.cpp), made at unit size
        // and scaled, so a cylinder or sphere may be elliptic.
        TriangleMesh mesh;
        wxString     shape_label;
        if (shape == "box") {
            mesh        = TriangleMesh(its_make_cube(1., 1., 1.));
            shape_label = _L("Cube");
        } else if (shape == "cylinder") {
            mesh        = TriangleMesh(its_make_cylinder(0.5, 1.));
            shape_label = _L("Cylinder");
        } else {
            mesh        = TriangleMesh(its_make_sphere(0.5, PI / 90));
            shape_label = _L("Sphere");
        }
        mesh.scale(size.cast<float>());

        // add_volume centres the mesh on the volume origin. Then, as the GUI does,
        // undo the copy's rotation and scale so the shape stays aligned with the bed,
        // and put its centre on the requested world position.
        ModelVolume *mv = mo->add_volume(std::move(mesh), type);
        const Geometry::Transformation &inst = mo->instances.front()->get_transformation();
        const Transform3d inst_inverse = inst.get_matrix_no_offset().inverse();
        mv->set_transformation(inst_inverse);
        mv->set_offset(inst_inverse * (position - inst.get_offset()));

        Slic3r::save_object_mesh(*mo); // the auto-backup copy of the object, as the GUI does
        mv->name = name.empty() ? into_u8(_L("Generic") + "-" + shape_label) : name;
        mv->config.assign_config(std::move(overrides));
        mv->source.is_from_builtin_objects = true;

        // Rebuild the object's rows in the object list (it sorts the volumes by type),
        // reload the 3D scene and reschedule slicing, as the GUI's add does.
        wxGetApp().obj_list()->reorder_volumes_and_get_selection(idx);
        invalidate_object_plates(idx);
        api_notify("Added " + mv->name + " to '" + mo->name + "'");

        const auto it = std::find(mo->volumes.begin(), mo->volumes.end(), mv);
        return {{"volume", volume_json(*mo, size_t(it - mo->volumes.begin()))}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object" || r["error"] == "no_instance") return { 404, r };
        return { 422, r }; // config_rejected
    }
    return { 201, r };
}

// PUT /objects/{id}/volumes/{vid}/config - same atomic semantics and key policy as
// PUT /objects/{id}/config, applied to one volume's own overrides.
Response Controller::handle_put_volume_config(uint64_t id, uint64_t vid, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    nlohmann::json r = run_on_ui([id, vid, in]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        const int vi = find_volume_index(*mo, vid);
        if (vi < 0) return {{"error", "unknown_volume"}};
        ModelVolume *mv = mo->volumes[vi];

        DynamicPrintConfig cfg = mv->config.get(); // copy of the volume's overrides
        nlohmann::json applied = nlohmann::json::array();
        nlohmann::json errors  = nlohmann::json::object();
        stage_object_config(cfg, in, applied, errors);
        if (!errors.empty()) { // atomic: apply nothing on any error
            api_notify("Couldn't set settings on '" + mv->name + "'", true);
            return {{"applied", nlohmann::json::array()}, {"errors", errors}};
        }
        mv->config.assign_config(std::move(cfg));
        plater->changed_object(idx);
        invalidate_object_plates(idx);

        // The object list draws the volume's filament and its settings row itself
        // (see handle_put_object_config), so refresh both.
        ObjectList          *obj_list = wxGetApp().obj_list();
        ObjectDataViewModel *om       = obj_list->GetModel();
        wxDataViewItem       item     = om->GetItemByVolumeId(idx, vi);
        if (item.IsOk()) {
            if (in.contains("extruder")) {
                const int ext = mv->config.has("extruder") ? mv->config.extruder() : 0;
                om->SetExtruder(ext == 0 ? _L("default") : wxString::Format("%d", ext), item);
            }
            obj_list->add_settings_item(item, &mv->config.get());
            obj_list->Refresh();
        }
        if (!applied.empty())
            api_notify("Updated " + std::to_string(applied.size()) + " setting(s) on '" + mv->name + "'");
        return {{"applied", applied}, {"errors", errors}, {"object", mo->name}, {"volume", volume_json(*mo, vi)}};
    });
    if (r.contains("error")) return { 404, r };
    return { r["errors"].empty() ? 200 : 422, r };
}

// DELETE /objects/{id}/volumes/{vid} - the object list's own delete
// (ObjectList::del_subobject_item), after refusing the cases where it would open a
// dialog: the last solid part (an error box) and a part of a cut object (a question).
Response Controller::handle_delete_volume(uint64_t id, uint64_t vid)
{
    nlohmann::json r = run_on_ui([id, vid]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        const int vi = find_volume_index(*mo, vid);
        if (vi < 0) return {{"error", "unknown_volume"}};
        const ModelVolume *mv = mo->volumes[vi];

        int parts = 0;
        for (const ModelVolume *v : mo->volumes)
            if (v->is_model_part()) ++parts;
        if (mv->is_model_part() && parts == 1)
            return {{"error", "last_part"}};
        if (mo->is_cut() && (mv->is_model_part() || mv->is_negative_volume()))
            return {{"error", "cut_object"}, {"detail", "parts of a cut object are removed in the window"}};

        // With a single volume the list shows no volume rows, but that case is the
        // last part, refused above.
        wxDataViewItem item = wxGetApp().obj_list()->GetModel()->GetItemByVolumeId(idx, vi);
        if (!item.IsOk()) return {{"error", "list_item_missing"}};
        const std::string vname = mv->name;
        wxGetApp().obj_list()->del_subobject_item(item);
        invalidate_object_plates(idx);
        api_notify("Removed '" + vname + "' from '" + mo->name + "'");
        return {{"ok", true}};
    });
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "unknown_object" || e == "unknown_volume") return { 404, r };
        if (e == "list_item_missing") return { 500, r };
        return { 409, r }; // last_part, cut_object
    }
    return { 200, r };
}

// --- POST /project/save {"path"?: "...", "overwrite"?: bool} ----------------
// Plater::save_project without its two dialogs: the Save As file picker (the path
// comes from the body instead) and the message box when writing fails.
Response Controller::handle_save_project(const std::string &body)
{
    std::string path;
    bool        overwrite = false;
    if (body.find_first_not_of(" \t\r\n") != std::string::npos) { // the body is optional
        nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
        if (!in.is_object())
            return { 400, {{"error", "body_must_be_object"}} };
        if (in.contains("path")) {
            if (!in["path"].is_string()) return { 400, {{"error", "path_must_be_string"}} };
            path = in["path"].get<std::string>();
        }
        if (in.contains("overwrite")) {
            if (!in["overwrite"].is_boolean()) return { 400, {{"error", "overwrite_must_be_boolean"}} };
            overwrite = in["overwrite"].get<bool>();
        }
    }

    // Big projects take a while to write (thumbnails + meshes): allow 120 s, not 10.
    nlohmann::json r = run_on_ui([path, overwrite]() -> nlohmann::json {
        Plater  *plater = wxGetApp().plater();
        // The window greys out Save and Save As while a G-code file or an exported
        // .gcode.3mf is open (MainFrame::can_save / can_save_as). Then the project name
        // is that file's, so a save would replace the sliced file with a plain project,
        // and with a path it would write an almost empty one. Refuse as POST /view does.
        if (plater->only_gcode_mode() || plater->using_exported_file())
            return {{"error", "preview_only"}};
        // The other half of can_save_as. For the brim-ears tool the call also writes the
        // ears being edited into the model, so the file gets them.
        if (plater->get_view3D_canvas3D()->get_gizmos_manager().is_in_editing_mode(false))
            return {{"error", "gizmo_editing"}};

        wxString filename;
        if (path.empty()) {
            // The project's own file: saving over it is what "Save" means.
            filename = plater->get_project_filename(".3mf");
            if (filename.IsEmpty()) return {{"error", "project_unnamed"}};
        } else {
            filename = wxString::FromUTF8(path);
            const boost::filesystem::path target = into_path(filename);
            if (!boost::iequals(target.extension().string(), ".3mf"))
                return {{"error", "not_3mf"}};
            if (!target.is_absolute() || !boost::filesystem::is_directory(target.parent_path()))
                return {{"error", "bad_path"}, {"detail", "an absolute path in an existing folder"}};
            if (boost::filesystem::exists(target) && !overwrite)
                return {{"error", "file_exists"}, {"detail", "pass \"overwrite\": true to replace it"}};
        }

        // Same strategy as Plater::save_project.
        SaveStrategy strategy = SaveStrategy::SplitModel | SaveStrategy::ShareMesh;
        if (wxGetApp().app_config->get_bool("export_sources_full_pathnames"))
            strategy = strategy | SaveStrategy::FullPathSources;
        if (plater->export_3mf(into_path(filename), strategy) < 0)
            return {{"error", "save_failed"}};

        // The rest of Plater::save_project: drop the crash-recovery backup, adopt the
        // file name, mark the project and its presets saved and refresh the title.
        Slic3r::remove_backup(plater->model(), false);
        plater->set_project_filename(filename);
        plater->up_to_date(true, false);
        plater->up_to_date(true, true);
        wxGetApp().update_saved_preset_from_current_preset();
        plater->reset_project_dirty_after_save();
        plater->update_title_dirty_status();

        const std::string saved = filename.ToUTF8().data();
        api_notify("Saved project " + saved);
        return {{"path", saved}};
    }, 120);
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "not_3mf" || e == "bad_path") return { 422, r };
        if (e == "save_failed")                return { 500, r };
        return { 409, r }; // preview_only, gizmo_editing, project_unnamed, file_exists
    }
    return { 200, r };
}

// --- POST /objects/{id}/paint ------------------------------------------------
//   {"layer": "support" | "seam" | "color",
//    "state": "enforce" | "block" | "none"   (support, seam)
//    "slot": n                               (color: 1-based filament slot),
//    "box_mm": [[x0,y0,z0], [x1,y1,z1]]      (optional, world mm),
//    "max_overhang_deg": a                   (optional, 0 <= a <= 90),
//    "clear": true                           (optional)}
// Paints whole triangles of the object's solid parts into the store the paint gizmos
// use (ModelVolume::supported_facets / seam_facets / mmu_segmentation_facets, through
// a TriangleSelector). A triangle is painted when its centroid, in world coordinates
// of the object's first copy, is inside the box (no box: every triangle) and, with
// max_overhang_deg, when its normal points down at least that many degrees below
// horizontal (unit normal n: n.z < 0 and -n.z >= sin(a)). So 90 keeps only flat
// undersides, 45 the usual overhangs that need support, 0 every downward face; this
// is the rule the spec and the MCP's paint_region tool state. "clear" (never with a
// box) first erases that layer on the whole object; with no state/slot it only erases.
Response Controller::handle_paint(uint64_t id, const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in dispatch
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };

    const std::string layer = in.contains("layer") && in["layer"].is_string() ? in["layer"].get<std::string>() : "";
    if (layer != "support" && layer != "seam" && layer != "color")
        return { 422, {{"error", "bad_layer"}, {"detail", "support | seam | color"}} };

    if (in.contains("clear") && !in["clear"].is_boolean())
        return { 400, {{"error", "clear_must_be_boolean"}} };
    const bool clear = in.value("clear", false);

    // What to paint with. Support and seam take a state; color takes a filament slot,
    // which TriangleSelector stores as Extruder1 + (slot - 1).
    bool                has_state = false;
    EnforcerBlockerType state     = EnforcerBlockerType::NONE;
    int                 slot      = 0;
    if (layer == "color") {
        if (in.contains("slot")) {
            if (!in["slot"].is_number_integer()) return { 422, {{"error", "bad_slot"}, {"detail", "1-based filament slot"}} };
            slot = in["slot"].get<int>();
            if (slot < 1 || slot > int(EnforcerBlockerType::ExtruderMax))
                return { 422, {{"error", "bad_slot"}, {"detail", "1-based filament slot"}} };
            state     = EnforcerBlockerType(int(EnforcerBlockerType::Extruder1) + slot - 1);
            has_state = true;
        }
    } else if (in.contains("state")) {
        const std::string s = in["state"].is_string() ? in["state"].get<std::string>() : "";
        if      (s == "enforce") state = EnforcerBlockerType::ENFORCER;
        else if (s == "block")   state = EnforcerBlockerType::BLOCKER;
        else if (s == "none")    state = EnforcerBlockerType::NONE;
        else return { 422, {{"error", "bad_state"}, {"detail", "enforce | block | none"}} };
        has_state = true;
    }

    bool  has_box = false;
    Vec3d box_min = Vec3d::Zero(), box_max = Vec3d::Zero();
    if (in.contains("box_mm")) {
        const nlohmann::json &b = in["box_mm"];
        Vec3d c0, c1;
        if (!b.is_array() || b.size() != 2 || !read_vec3(b[0], c0) || !read_vec3(b[1], c1))
            return { 422, {{"error", "bad_box"}, {"detail", "box_mm: [[x0,y0,z0], [x1,y1,z1]] in world mm"}} };
        box_min = c0.cwiseMin(c1);
        box_max = c0.cwiseMax(c1);
        has_box = true;
    }
    // "clear" erases the layer on the whole object (the spec gives it no box). With a box
    // a caller most likely means "redo this spot" and would silently lose every other
    // stroke, so refuse it, as the MCP does; state "none" erases inside a box.
    if (clear && has_box)
        return { 422, {{"error", "clear_takes_no_box"},
                       {"detail", "clear erases the layer on the whole object; to erase inside box_mm "
                                  "use \"state\": \"none\" (support, seam)"}} };

    bool   has_overhang = false;
    double max_overhang_deg = 0.;
    if (in.contains("max_overhang_deg")) {
        max_overhang_deg = in["max_overhang_deg"].is_number() ? in["max_overhang_deg"].get<double>() : -1.;
        if (!(max_overhang_deg >= 0. && max_overhang_deg <= 90.))
            return { 422, {{"error", "bad_overhang"}, {"detail", "0 <= max_overhang_deg <= 90, degrees below horizontal"}} };
        has_overhang = true;
    }

    if (!has_state && !clear)
        return { 422, {{"error", layer == "color" ? "bad_slot" : "bad_state"},
                       {"detail", layer == "color" ? "give \"slot\" (or \"clear\": true)" : "give \"state\" (or \"clear\": true)"}} };
    if (!has_state && (has_box || has_overhang))
        return { 422, {{"error", layer == "color" ? "bad_slot" : "bad_state"},
                       {"detail", "box_mm and max_overhang_deg choose where to paint; say with what"}} };

    nlohmann::json r = run_on_ui([=]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        const int idx = find_object_index(plater->model(), id);
        if (idx < 0) return {{"error", "unknown_object"}};
        ModelObject *mo = plater->model().objects[idx];
        if (mo->instances.empty()) return {{"error", "no_instance"}};
        if (slot > wxGetApp().filaments_cnt())
            return {{"error", "bad_slot"}, {"detail", "the project has " + std::to_string(wxGetApp().filaments_cnt()) + " filament(s)"}};

        // An open paint gizmo holds its own copy of the paint and writes it back on its
        // next stroke, so close it first, as the object list does before it erases
        // support paint (ObjectList::del_info_item).
        GLCanvas3D *canvas = plater->get_view3D_canvas3D();
        canvas->get_gizmos_manager().reset_all_states();
        plater->take_snapshot("Paint " + layer);

        const Transform3d instance_matrix = mo->instances.front()->get_matrix();
        // The 1e-6 slack keeps 90 working on a rotated copy, whose flat undersides
        // come out a hair off -Z from float rounding (1e-6 is under 0.1 degree there).
        const double      sin_limit       = std::sin(max_overhang_deg * PI / 180.) - 1e-6;
        int painted = 0;
        for (ModelVolume *mv : mo->volumes) {
            if (!mv->is_model_part()) continue; // the gizmos paint solid parts only
            FacetsAnnotation &facets = layer == "support" ? mv->supported_facets :
                                       layer == "seam"    ? mv->seam_facets :
                                                            mv->mmu_segmentation_facets;
            if (clear)
                facets.reset();
            if (!has_state)
                continue;

            const TriangleMesh &mesh  = mv->mesh();
            const Transform3d   trafo = instance_matrix * mv->get_matrix();
            // A mirrored copy reverses the triangles' winding, so their normals flip.
            const bool mirrored = trafo.linear().determinant() < 0.;
            TriangleSelector selector(mesh);
            selector.deserialize(facets.get_data()); // keep the paint already there
            for (int f = 0; f < (int) mesh.its.indices.size(); ++f) {
                const auto &tri = mesh.its.indices[f]; // three vertex indices
                const Vec3d a = trafo * mesh.its.vertices[tri[0]].cast<double>();
                const Vec3d b = trafo * mesh.its.vertices[tri[1]].cast<double>();
                const Vec3d c = trafo * mesh.its.vertices[tri[2]].cast<double>();
                const Vec3d centroid = (a + b + c) / 3.;
                if (has_box && !((centroid.array() >= box_min.array()).all() && (centroid.array() <= box_max.array()).all()))
                    continue;
                if (has_overhang) {
                    Vec3d normal = (b - a).cross(c - a);
                    if (mirrored) normal = -normal;
                    const double length = normal.norm();
                    // The normal must point down (vertical walls never count) and at
                    // least max_overhang_deg below horizontal.
                    if (length <= 0. || normal.z() >= 0. || -normal.z() / length < sin_limit)
                        continue;
                }
                selector.set_facet(f, state);
                ++painted;
            }
            facets.set(selector);
        }

        // What the gizmos do after a stroke (update_model_object): refresh the object's
        // info rows ("Support painting", "Color painting"...) and reschedule slicing.
        wxGetApp().obj_list()->update_info_items(idx);
        plater->schedule_background_process();
        invalidate_object_plates(idx);
        canvas->set_as_dirty(); // color paint shows in the 3D view on the next frame
        api_notify("Painted " + std::to_string(painted) + " triangle(s) (" + layer + ") on '" + mo->name + "'" +
                   (clear ? " after clearing" : ""));
        return {{"painted_triangles", painted}, {"layer", layer}, {"cleared", clear}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object" || r["error"] == "no_instance") return { 404, r };
        return { 422, r }; // bad_slot
    }
    return { 200, r };
}

}}} // namespace
