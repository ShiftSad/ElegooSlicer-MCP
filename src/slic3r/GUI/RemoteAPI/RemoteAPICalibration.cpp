// src/slic3r/GUI/RemoteAPI/RemoteAPICalibration.cpp
//
// elegoo-mcp: POST /calibration, the window's Calibration menu without its dialogs.
// Each menu entry opens a small dialog (calib_dlg.cpp) with the test's settings; its
// OK button checks them and calls one of the Plater::calib_* functions, which replace
// the project with a new one holding the test model and the settings the test needs
// (nothing is sliced). The API takes the dialog's fields in "params", checks them the
// way the dialog does, and calls the same function.
#include "RemoteAPIController.hpp"

#include "libslic3r/Model.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/calib.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/Plater.hpp"

#include <algorithm>
#include <cmath>
#include <sstream>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// One calibration test as the request describes it, with the dialog's defaults for
// whatever the client left out.
struct CalibTest
{
    std::string   type;
    Calib_Params  params;                          // every test except flow_rate
    bool          flow_linear  = true;             // flow_rate: YOLO (linear) or the classic passes
    int           flow_pass    = 1;                // flow_rate: 1 or 2
    InfillPattern flow_pattern = ipArchimedeanChords;

    CalibTest()
    {
        // Calib_Params leaves its numbers uninitialised; the tests read only some of them.
        params.start = params.end = params.step = 0.;
        params.print_numbers = false;
        params.freqStartX = params.freqEndX = params.freqStartY = params.freqEndY = 0.;
        params.test_model = 0;
    }
};

// A number as the error details show it: "0.002", not "0.002000".
static std::string num(double v)
{
    std::ostringstream ss;
    ss << v;
    return ss.str();
}

// The parameter names a test accepts. A misspelt name would otherwise be ignored
// silently and the test run with a default the client did not ask for.
static std::string unknown_param(const nlohmann::json &params, const std::vector<std::string> &known)
{
    for (auto it = params.begin(); it != params.end(); ++it) {
        if (std::find(known.begin(), known.end(), it.key()) != known.end())
            continue;
        std::string list;
        for (const std::string &k : known)
            list += (list.empty() ? "" : ", ") + k;
        return it.key() + ": unknown parameter (" + (list.empty() ? std::string("this test takes none") : "known: " + list) + ")";
    }
    return {};
}

// params[name] as a number, or `fallback` when it is not there. Returns false when it
// is there but not a finite number.
static bool number_param(const nlohmann::json &params, const std::string &name, double fallback, double &out)
{
    out = fallback;
    if (!params.contains(name))
        return true;
    if (!params[name].is_number())
        return false;
    out = params[name].get<double>();
    return std::isfinite(out);
}

// params[name] as a string, or `fallback`. Returns false when it is not a string.
static bool string_param(const nlohmann::json &params, const std::string &name, const std::string &fallback, std::string &out)
{
    out = fallback;
    if (!params.contains(name))
        return true;
    if (!params[name].is_string())
        return false;
    out = params[name].get<std::string>();
    return true;
}

// Pressure advance (PA_Calibration_Dlg): a tower, lines or a pattern. The extruder
// type only picks the defaults, as the dialog's radio buttons do. Returns "" or the
// reason the parameters are refused.
static std::string read_pressure_advance(const nlohmann::json &params, CalibTest &test)
{
    std::string e = unknown_param(params, {"method", "extruder", "start", "end", "step", "print_numbers", "accelerations", "speeds"});
    if (!e.empty()) return e;
    std::string method, extruder;
    if (!string_param(params, "method", "tower", method) || (method != "tower" && method != "line" && method != "pattern"))
        return "method: \"tower\" | \"line\" | \"pattern\"";
    if (!string_param(params, "extruder", "dde", extruder) || (extruder != "dde" && extruder != "bowden"))
        return "extruder: \"dde\" | \"bowden\"";

    // PA_Calibration_Dlg::reset_params
    const bool bowden = extruder == "bowden";
    double def_end  = method == "pattern" ? 0.08 : 0.1;
    double def_step = method == "pattern" ? 0.005 : 0.002;
    if (bowden) {
        def_end  = 1.0;
        def_step = method == "pattern" ? 0.05 : 0.02;
    }
    Calib_Params &p = test.params;
    if (!number_param(params, "start", 0.0, p.start)) return "start: a number";
    if (!number_param(params, "end", def_end, p.end)) return "end: a number";
    if (!number_param(params, "step", def_step, p.step)) return "step: a number";
    // PA_Calibration_Dlg::on_start
    if (p.start < 0) return "start: must be >= 0";
    if (p.step < 10 * EPSILON) return "step: must be >= " + num(10 * EPSILON);
    if (p.end < p.start + p.step) return "end: must be >= start + step (" + num(p.start + p.step) + ")";

    // "Print numbers" is a choice for lines only: always on for the pattern, off for the tower.
    p.print_numbers = method != "tower";
    if (params.contains("print_numbers")) {
        if (!params["print_numbers"].is_boolean()) return "print_numbers: true | false";
        if (method != "line") return "print_numbers: only for \"method\": \"line\"";
        p.print_numbers = params["print_numbers"].get<bool>();
    }

    // Accelerations and speeds (mm/s^2, mm/s): the pattern prints one test per pair. The
    // dialog takes positive whole numbers; with neither, the test uses the print
    // settings' outer wall acceleration and an optimal speed.
    p.accelerations.clear();
    p.speeds.clear();
    for (const char *name : {"accelerations", "speeds"}) {
        if (!params.contains(name)) continue;
        if (method != "pattern") return std::string(name) + ": only for \"method\": \"pattern\"";
        const nlohmann::json &list = params[name];
        std::vector<double>  &out  = std::string(name) == "speeds" ? p.speeds : p.accelerations;
        if (!list.is_array()) return std::string(name) + ": a list of positive whole numbers";
        for (const nlohmann::json &v : list) {
            if (!v.is_number_integer() || v.get<long long>() <= 0)
                return std::string(name) + ": a list of positive whole numbers";
            out.push_back(double(v.get<long long>()));
        }
    }
    if (!p.accelerations.empty() && !p.speeds.empty() &&
        *std::min_element(p.accelerations.begin(), p.accelerations.end()) <= *std::max_element(p.speeds.begin(), p.speeds.end()))
        return "accelerations: every acceleration must be greater than every speed (are the two lists swapped?)";

    p.mode = method == "line" ? CalibMode::Calib_PA_Line : method == "pattern" ? CalibMode::Calib_PA_Pattern : CalibMode::Calib_PA_Tower;
    return {};
}

// Flow ratio (FlowRateCalibrationDialog): the test type and the top surface pattern.
static std::string read_flow_rate(const nlohmann::json &params, CalibTest &test)
{
    std::string e = unknown_param(params, {"test", "pattern"});
    if (!e.empty()) return e;
    std::string kind, pattern;
    if (!string_param(params, "test", "yolo", kind) ||
        (kind != "yolo" && kind != "yolo_perfectionist" && kind != "pass1" && kind != "pass2"))
        return "test: \"yolo\" | \"yolo_perfectionist\" | \"pass1\" | \"pass2\"";
    if (!string_param(params, "pattern", "archimedean_chords", pattern) || (pattern != "archimedean_chords" && pattern != "monotonic"))
        return "pattern: \"archimedean_chords\" | \"monotonic\"";
    // FlowRateCalibrationDialog::on_start: the YOLO tests are the "linear" ones.
    test.flow_linear  = kind == "yolo" || kind == "yolo_perfectionist";
    test.flow_pass    = kind == "pass2" || kind == "yolo_perfectionist" ? 2 : 1;
    test.flow_pattern = pattern == "monotonic" ? ipMonotonic : ipArchimedeanChords;
    return {};
}

// Nozzle temperature tower (Temp_Calibration_Dlg): from `start` at the bottom down to
// `end`, 5 degrees per block.
static std::string read_temperature(const nlohmann::json &params, CalibTest &test)
{
    std::string e = unknown_param(params, {"start", "end"});
    if (!e.empty()) return e;
    Calib_Params &p = test.params;
    if (!number_param(params, "start", 230, p.start)) return "start: a number (degrees C)";
    if (!number_param(params, "end", 190, p.end)) return "end: a number (degrees C)";
    // The dialog's fields round to multiples of 5 within 155..500 (validate_text), then
    // on_start wants start <= 500, end >= 155 and start >= end + 5.
    for (const char *name : {"start", "end"}) {
        const double v = std::string(name) == "start" ? p.start : p.end;
        if (v != std::floor(v) || std::fmod(v, 5.0) != 0.0) return std::string(name) + ": a multiple of 5";
        if (v < 155 || v > 500) return std::string(name) + ": between 155 and 500";
    }
    if (p.start < p.end + 5) return "start: must be >= end + 5 (the tower goes from start down to end)";
    p.step = 5; // the tower's fixed block step (GCode.cpp interpolates 5 degrees per block)
    p.mode = CalibMode::Calib_Temp_Tower;
    return {};
}

// The start/end/step towers: max volumetric speed, VFA and retraction. Their dialogs
// share one check; only the defaults and the lowest start differ.
struct RangeTest
{
    double      def_start, def_end, def_step;
    double      min_start;           // start must be above this...
    bool        start_may_be_min;    // ...or equal to it
    const char *unit;
    CalibMode   mode;
};

static std::string read_range(const nlohmann::json &params, const RangeTest &t, CalibTest &test)
{
    std::string e = unknown_param(params, {"start", "end", "step"});
    if (!e.empty()) return e;
    Calib_Params &p = test.params;
    const std::string unit = std::string(" (") + t.unit + ")";
    if (!number_param(params, "start", t.def_start, p.start)) return "start: a number" + unit;
    if (!number_param(params, "end", t.def_end, p.end)) return "end: a number" + unit;
    if (!number_param(params, "step", t.def_step, p.step)) return "step: a number" + unit;
    if (t.start_may_be_min ? p.start < t.min_start : p.start <= t.min_start)
        return "start: must be " + std::string(t.start_may_be_min ? ">= " : "> ") + num(t.min_start);
    if (p.step <= 0) return "step: must be > 0";
    if (p.end < p.start + p.step) return "end: must be >= start + step (" + num(p.start + p.step) + ")";
    p.mode = t.mode;
    return {};
}

// POST /calibration {"type", "params"?: {...}, "discard_project"?: bool}
Response Controller::handle_calibration(const std::string &body)
{
    nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_project
    if (!in.is_object())
        return { 400, {{"error", "body_must_be_object"}} };
    if (in.contains("discard_project") && !in["discard_project"].is_boolean())
        return { 400, {{"error", "discard_project_must_be_boolean"}} };
    if (in.contains("params") && !in["params"].is_object())
        return { 422, {{"error", "bad_params"}, {"detail", "params: an object"}} };
    const nlohmann::json params  = in.value("params", nlohmann::json::object());
    const bool           discard = in.value("discard_project", false);

    CalibTest test;
    test.type = in.contains("type") && in["type"].is_string() ? in["type"].get<std::string>() : "";
    // Defaults and lowest starts from the dialogs: MaxVolumetricSpeed_Test_Dlg (mm3/s),
    // VFA_Test_Dlg (mm/s), Retraction_Test_Dlg (mm).
    static const RangeTest max_volumetric_speed { 5, 20, 0.5, 0, false, "mm3/s", CalibMode::Calib_Vol_speed_Tower };
    static const RangeTest vfa                  { 40, 200, 10, 10, false, "mm/s", CalibMode::Calib_VFA_Tower };
    static const RangeTest retraction           { 0, 2, 0.1, 0, true, "mm", CalibMode::Calib_Retraction_tower };
    std::string error;
    if (test.type == "pressure_advance")          error = read_pressure_advance(params, test);
    else if (test.type == "flow_rate")            error = read_flow_rate(params, test);
    else if (test.type == "temperature")          error = read_temperature(params, test);
    else if (test.type == "max_volumetric_speed") error = read_range(params, max_volumetric_speed, test);
    else if (test.type == "vfa")                  error = read_range(params, vfa, test);
    else if (test.type == "retraction")           error = read_range(params, retraction, test);
    else
        return { 422, {{"error", "bad_type"},
                       {"detail", "type: flow_rate | pressure_advance | temperature | retraction | max_volumetric_speed | vfa"}} };
    if (!error.empty())
        return { 422, {{"error", "bad_params"}, {"detail", error}} };

    // Loading the test model and cutting it to height takes a few seconds: allow 60 s.
    nlohmann::json r = run_on_ui([this, test, discard]() -> nlohmann::json {
        Plater       *plater = wxGetApp().plater();
        PresetBundle *bundle = wxGetApp().preset_bundle;
        // A new project cancels a running arrange/orient job without waiting for it, and
        // stops a running slice under GET /slice/status's feet.
        if (!plater->get_ui_job_worker().is_idle()) return {{"error", "job_running"}};
        if (plater->is_background_process_slicing()) return {{"error", "slicing"}};

        // Every calib_* starts with Plater::new_project, which asks before throwing
        // anything away (close_with_confirm): "save the project?" when it has unsaved
        // changes, then "keep, discard or save the modified presets?". An API call cannot
        // answer, so refuse, unless the client said to discard both.
        const bool     project_unsaved = !plater->up_to_date(false, false);
        nlohmann::json presets_unsaved = nlohmann::json::array();
        if (bundle->prints.current_is_dirty())    presets_unsaved.push_back("print");
        if (bundle->filaments.current_is_dirty()) presets_unsaved.push_back("filament");
        if (bundle->printers.current_is_dirty())  presets_unsaved.push_back("printer");
        const bool unsaved = project_unsaved || !presets_unsaved.empty();
        if (unsaved && !discard)
            return {{"error", "unsaved_project"}, {"project_unsaved", project_unsaved}, {"presets_unsaved", presets_unsaved},
                    {"hint", "save the project (POST /project/save) and presets (POST /preset/save) first, "
                             "or pass \"discard_project\": true to drop the changes"}};

        // With discard_project, the questions' "Don't save" and "Discard" answers: drop the
        // preset edits (as check_and_keep_current_preset_changes does for "Discard").
        nlohmann::json discarded = nlohmann::json::array();
        if (unsaved) {
            if (project_unsaved) discarded.push_back("project");
            PresetCollection *collections[] = {&bundle->prints, &bundle->filaments, &bundle->printers};
            for (PresetCollection *presets : collections)
                if (presets->current_is_dirty())
                    presets->discard_current_changes();
            for (const auto &group : presets_unsaved)
                discarded.push_back(group);
        }
        // Start an empty project without asking (nothing is left to ask about). The
        // calib_* call below starts its own new project from this clean state, so its
        // questions do not come up either.
        plater->new_project(/*skip_confirm=*/true, /*silent=*/true);

        const Calib_Params &p = test.params;
        if (test.type == "pressure_advance")          plater->calib_pa(p);
        else if (test.type == "flow_rate")            plater->calib_flowrate(test.flow_linear, test.flow_pass, test.flow_pattern);
        else if (test.type == "temperature")          plater->calib_temp(p);
        else if (test.type == "max_volumetric_speed") plater->calib_max_vol_speed(p);
        else if (test.type == "vfa")                  plater->calib_VFA(p);
        else                                          plater->calib_retraction(p);

        nlohmann::json objects = nlohmann::json::array();
        for (const ModelObject *mo : plater->model().objects)
            objects.push_back({{"id", (uint64_t) mo->id().id}, {"name", mo->name}});
        // The new project has new plates: /slice/status must stop describing the old one.
        sync_slice_state_to_current_plate("slice.plate_changed");
        if (objects.empty())
            return {{"error", "calibration_failed"}, {"detail", "the test model did not load"}};
        api_notify("Calibration test created: " + test.type);
        nlohmann::json reply = {{"type", test.type}, {"objects", objects}};
        if (!discarded.empty())
            reply["discarded_changes"] = discarded;
        return reply;
    }, 60);
    if (r.contains("error")) {
        const std::string e = r["error"].get<std::string>();
        if (e == "calibration_failed") return { 500, r };
        return { 409, r }; // unsaved_project, job_running, slicing
    }
    return { 200, r };
}

}}} // namespace
