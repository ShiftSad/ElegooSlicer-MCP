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
#include "slic3r/GUI/MainFrame.hpp"
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

}}} // namespace
