// src/slic3r/GUI/RemoteAPI/RemoteAPIModelOps.cpp
//
// elegoo-mcp: model operations ElegooSlicer MCP adds to the Remote API.
// Each handler runs its GUI work inside run_on_ui and repeats the steps of the
// matching GUI action, leaving out that action's dialogs: an API call must never
// open a modal window, because nobody may be at the machine to close it.
#include "RemoteAPIController.hpp"

#include "libslic3r/Model.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/GLCanvas3D.hpp"
#include "slic3r/GUI/GUI_ObjectList.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/Selection.hpp"

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

        // Select only this object, the way Plater::priv::load_files selects new objects,
        // and let the object list follow the 3D scene.
        GLCanvas3D *canvas = plater->get_view3D_canvas3D();
        canvas->get_selection().add_object((unsigned int) idx, /*as_single_selection=*/true);
        wxGetApp().obj_list()->update_selections();
        if (canvas->get_gizmos_manager().is_enabled())
            canvas->update_gizmos_on_off_state();

        // Same reset as handle_orient: Elegoo's sticky "outside bed" prepare state makes
        // the job skip the selection.
        plater->set_prepare_state(Job::PREPARE_STATE_DEFAULT);
        plater->orient();
        return {{"started", true}, {"object_id", id}};
    });
    if (r.contains("error")) {
        if (r["error"] == "unknown_object") return { 404, r };
        return { 409, r }; // job_running
    }
    return { 202, r };
}

}}} // namespace
