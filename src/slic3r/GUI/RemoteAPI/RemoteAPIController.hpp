// src/slic3r/GUI/RemoteAPI/RemoteAPIController.hpp
#ifndef slic3r_RemoteAPIController_hpp_
#define slic3r_RemoteAPIController_hpp_

#include "RemoteAPIServer.hpp"

#include <functional>
#include <mutex>
#include <set>

namespace Slic3r { class Model; class DynamicPrintConfig; }

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// elegoo-mcp: helpers RemoteAPIController.cpp shares with RemoteAPIModelOps.cpp.
// GUI thread only (call them inside run_on_ui).
int  find_object_index(const Model &model, uint64_t id); // index in Model::objects, -1 if none
void api_notify(const std::string &text, bool warning = false); // on-screen toast
std::string main_view_name(); // main-window tab: "prepare" | "preview" | "other"
void invalidate_object_plates(int obj_idx); // mark the plates holding the object unsliced
// Stages per-object/per-part overrides from a JSON body onto cfg with the policy of
// every config write (unknown, sensitive and read-only keys are refused into errors).
void stage_object_config(DynamicPrintConfig &cfg, const nlohmann::json &in,
                         nlohmann::json &applied, nlohmann::json &errors);

// Snapshot of the last/current slice, maintained on the GUI thread by wx event
// subscriptions (Task 10), read by API threads under the mutex.
struct SliceState
{
    std::string    state { "idle" };  // idle | slicing | done | error
    int            percent { -1 };
    std::string    message;           // progress text or error message
    nlohmann::json stats;             // filled on success (Task 10)
    nlohmann::json warnings = nlohmann::json::array();
};

class Controller
{
public:
    // Must be constructed on the GUI thread (binds wx events in Task 10).
    Controller();
    ~Controller();

    Response dispatch(const Request &req);

    // GUI-code hooks, callable even when the API is disabled (no-op when no
    // controller exists). preset_type is int to keep Tab.cpp include-light.
    static void notify_config_changed(int preset_type);
    static void notify_project_opened();

    // F8 mutual-exclusion gates between API UI mutations and the periodic
    // auto-backup 3MF exporter (which pumps the event queue mid-export, so the
    // two can interleave on the GUI thread and corrupt what is being written).
    // GUI thread only.
    static bool api_ui_task_active();          // an API task is on the stack
    static void set_backup_in_progress(bool);  // MainFrame brackets export_3mf;
                                               // false flushes deferred API tasks

    SliceState slice_state() const { std::lock_guard<std::mutex> lk(m_mutex); return m_slice; }

    // Call once on the GUI thread after the plater exists (from start_remote_api()).
    // Idempotent: guarded by m_events_bound.
    void bind_plater_events();

private:
    Response handle_status();
    Response handle_get_config(const std::string &target);
    Response handle_put_config(const std::string &body);
    Response handle_slice();
    Response handle_slice_status();
    Response handle_slice_cancel();   // F2: POST /slice/cancel - unwedge/abort
    Response handle_load_model(const std::string &body);   // M4a: POST /model
    Response handle_select_preset(const std::string &body); // M4a: PUT /preset
    Response handle_save_preset(const std::string &body);   // POST /preset/save
    Response handle_get_presets();                          // GET /presets
    Response handle_get_preset_config(const std::string &body); // POST /preset/config (read named preset)
    Response handle_delete_preset(const std::string &body);      // DELETE /preset
    Response handle_put_layer_height(uint64_t id, const std::string &body);  // M4c: PUT /objects/{id}/layer_height
    Response handle_put_height_range(uint64_t id, const std::string &body);  // M4c: PUT /objects/{id}/height_range
    Response handle_get_gcode();                            // M4a: GET /gcode (raw body)
    Response handle_plate_render(const std::string &target); // GET /plate/render (PNG: editor|preview)
    Response handle_get_objects();                          // M4b: GET /objects
    Response handle_delete_object(uint64_t id);             // M4b: DELETE /objects/{id}
    Response handle_transform_object(uint64_t id, const std::string &body); // M4b: POST /objects/{id}/transform
    Response handle_duplicate_object(uint64_t id);          // M4b: POST /objects/{id}/duplicate
    Response handle_put_object_config(uint64_t id, const std::string &body); // M4c: PUT /objects/{id}/config
    Response handle_arrange();                              // M4b: POST /arrange (async)
    Response handle_orient();                               // M4b: POST /orient (async)
    Response handle_jobs_status();                          // M4b: GET /jobs/status
    Response handle_get_plates();                           // plates: GET /plates
    Response handle_create_plate(const std::string &body);  // plates: POST /plates
    Response handle_select_plate(int index);                // plates: POST /plates/{i}/select
    Response handle_delete_plate(int index);                // plates: DELETE /plates/{i}
    Response handle_put_object_plate(uint64_t id, const std::string &body); // plates: POST /objects/{id}/plate

    // elegoo-mcp: model operations, defined in RemoteAPIModelOps.cpp.
    Response handle_orient_object(const std::string &body);  // POST /orient {"object_id": id}
    Response handle_set_view(const std::string &body);       // POST /view {"view"}
    // /objects/{id}/volumes[/{vid}[/config]]: action is the path after "{id}/".
    Response route_volumes(const Request &req, uint64_t id, const std::string &action);
    Response handle_get_volumes(uint64_t id);
    Response handle_add_volume(uint64_t id, const std::string &body);
    Response handle_put_volume_config(uint64_t id, uint64_t vid, const std::string &body);
    Response handle_delete_volume(uint64_t id, uint64_t vid);
    Response handle_save_project(const std::string &body);   // POST /project/save
    Response handle_paint(uint64_t id, const std::string &body); // POST /objects/{id}/paint

    // elegoo-mcp: Elegoo printers and CANVAS, defined in RemoteAPIElegoo.cpp.
    // {id} is the printer's API id (see api_printer_id there).
    Response route_elegoo(const Request &req);            // every /api/v1/printers and /api/v1/uploads route
    Response handle_get_printers();                       // GET /printers
    Response handle_get_printer(const std::string &id);   // GET /printers/{id}
    Response handle_get_printer_status(const std::string &id, const std::string &query); // GET /printers/{id}/status
    Response handle_get_printer_history(const std::string &id, const std::string &what,  // GET /printers/{id}/tasks|files
                                        const std::string &query);
    Response handle_get_canvas(const std::string &id);    // GET /printers/{id}/canvas
    Response handle_canvas_sync(const std::string &id, const std::string &body);     // POST /printers/{id}/canvas/sync
    Response handle_canvas_mapping(const std::string &id, const std::string &query); // GET /printers/{id}/canvas/mapping
    // Print control, also in RemoteAPIElegoo.cpp. Off unless the user allows it in Preferences;
    // check_print_control makes the checks every print-control route starts with.
    bool     check_print_control(const std::string &what, Response &refused);
    Response handle_print_prepare(const std::string &id, const std::string &body); // POST /printers/{id}/print/prepare
    Response handle_print_start(const std::string &id, const std::string &body);   // POST /printers/{id}/print
    Response handle_get_upload(const std::string &upload_id);                      // GET /uploads/{id}
    Response handle_cancel_upload(const std::string &upload_id);                   // DELETE /uploads/{id}
    Response handle_print_command(const std::string &id, const std::string &command, // POST /printers/{id}/pause|resume|stop
                                  const std::string &body);

    // Mutate m_slice under the lock and broadcast a snapshot (event_name) to WS clients.
    void set_slice_state(const std::function<void(SliceState&)> &mut, const char *event_name);
    // plates: make the stored slice state describe the current plate (GUI thread).
    void sync_slice_state_to_current_plate(const char *event_name);

    // Debounced config.changed emitter (GUI thread; mutex guards the pending set).
    void on_config_changed(int preset_type);

    // Runs fn on the GUI thread, blocks the calling (io) thread up to
    // timeout_s seconds (default 10). Throws std::runtime_error("ui_timeout")
    // on expiry.
    nlohmann::json run_on_ui(std::function<nlohmann::json()> fn, int timeout_s = 10);

    mutable std::mutex m_mutex;
    SliceState         m_slice;
    bool               m_events_bound { false };

    std::mutex         m_cc_mutex;
    std::set<int>      m_cc_pending;
    bool               m_cc_timer_armed { false };
};

}}} // namespace

#endif
