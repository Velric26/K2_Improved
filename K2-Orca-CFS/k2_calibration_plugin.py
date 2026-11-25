# /// script
# requires-python = ">=3.12"
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "K2 Calibration"
# description = "Run Auto Max Flow, Auto PA, Adaptive PA and Max Flow then Adaptive PA on the Creality K2."
# version = "1.7.0"
# ///

import http.client
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

import orca


COMPANION_PORT = 8766
MODES = {"max_flow": "Auto Max Flow", "pressure_advance": "Auto PA",
         "flow_then_pa": "Max Flow then Adaptive PA", "adaptive_pa": "Adaptive PA"}


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { padding: 24px; margin: 0; }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 6px; }
  h2 { font-size: 17px; margin: 20px 0 10px; }
  p { line-height: 1.5; }
  #printer { margin: 0 0 18px; }
  .fields { display: grid; gap: 12px; grid-template-columns: 2fr 1fr; }
  label { display: grid; gap: 5px; min-width: 0; }
  select { width: 100%; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0; }
  .actions button { padding: 7px 12px; }
  .actions { align-items: flex-start; }
  .actions button.combo { padding: 8px 14px; line-height: 1.35; min-height: 4.1em; text-align: center; }
  .actions button.combo small { display: block; font-size: 12px; opacity: .8; }
  #status { min-height: 1.5em; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.5; }
  @media (max-width: 460px) { .fields { grid-template-columns: 1fr; } }
</style>
<main>
  <h1>Auto Calibrations</h1>
  <p id="printer">Connecting to the K2 companion…</p>
  <div class="fields">
    <label>Orca filament preset<select id="preset" aria-describedby="preset-note"></select></label>
    <label>CFS slot<select id="slot"></select></label>
  </div>
  <p id="preset-note" hidden></p>
  <div class="actions">
    <button type="button" id="flow">Auto Max Flow</button>
    <button type="button" id="pa">Auto PA</button>
    <button type="button" id="adaptive">Adaptive PA</button>
    <button type="button" id="both" class="combo">1 · Auto Max Flow<small>then</small>2 · Adaptive PA</button>
  </div>
  <div class="actions">
    <button type="button" id="refresh">Refresh presets and slots</button>
    <button type="button" id="cancel" disabled>Cancel</button>
  </div>
  <p id="status" role="status" aria-live="polite"></p>
  <section id="result" hidden>
    <h2>Result</h2>
    <pre id="report"></pre>
    <button type="button" id="apply" hidden>Apply to filament preset</button>
  </section>
</main>
<script>
  const el = id => document.getElementById(id);
  const send = (type, extra={}) => window.orca.postMessage({type, ...extra});
  let state = {profiles: [], slots: [], busy: true};
  const chosenPreset = () => state.profiles.find(p => p.name === el('preset').value);
  function updateSlots(previous) {
    const preset = chosenPreset();
    const slots = preset && !preset.issue ? state.slots.filter(s => s.material === preset.material) : [];
    el('preset-note').textContent = preset?.issue || '';
    el('preset-note').hidden = !preset?.issue;
    el('slot').replaceChildren();
    el('slot').add(new Option('Select a CFS slot', ''));
    for (const s of slots) {
      el('slot').add(new Option(s.slot + ' · ' + s.material + (s.loaded ? ' · Loaded' : ''), s.slot));
    }
    if (slots.some(s => s.slot === previous)) el('slot').value = previous;
    updateButtons();
  }
  function updateButtons() {
    const slot = state.slots.find(s => s.slot === el('slot').value);
    const preset = chosenPreset();
    const ready = !state.busy && state.calibration_supported && state.printer &&
      slot && preset && !preset.issue && slot.material === preset.material;
    const flow = ready && slot.max_flow;
    el('flow').disabled = !flow;
    el('pa').disabled = !ready || preset.adaptive;
    el('adaptive').disabled = !ready || !state.adaptive_supported;
    el('both').disabled = !flow || !state.adaptive_supported;
    el('preset').disabled = state.busy || !state.profiles.length;
    el('slot').disabled = state.busy || !preset || Boolean(preset.issue);
    el('refresh').disabled = state.busy;
    el('cancel').disabled = !state.can_cancel;
    el('apply').disabled = state.busy;
  }
  el('preset').onchange = () => updateSlots('');
  el('slot').onchange = updateButtons;
  for (const [id, mode] of Object.entries({flow:'max_flow', pa:'pressure_advance',
                                           adaptive:'adaptive_pa', both:'flow_then_pa'})) {
    el(id).onclick = () => {
      send('start', {mode, preset:el('preset').value, slot:el('slot').value});
    };
  }
  for (const type of ['refresh','cancel','apply']) el(type).onclick = () => send(type);
  window.orca.onMessage(message => {
    const oldPreset = el('preset').value, oldSlot = el('slot').value;
    state = message;
    const printer = state.printer ? 'Creality K2 · ' + state.printer : 'K2 companion unavailable';
    el('printer').textContent = printer + (state.nozzle ? ' · ' + state.nozzle + ' mm nozzle' : '');
    el('preset').replaceChildren();
    for (const p of state.profiles) {
      const temperature = p.temperature ? ' · ' + p.temperature + ' °C' : '';
      el('preset').add(new Option(p.name + temperature, p.name));
    }
    if (state.profiles.some(p => p.name === oldPreset)) el('preset').value = oldPreset;
    else if (state.default_preset) el('preset').value = state.default_preset;
    updateSlots(oldSlot);
    el('status').textContent = state.status || '';
    el('result').hidden = !state.report;
    el('report').textContent = state.report || '';
    el('apply').hidden = !state.can_apply;
    updateButtons();
  });
  updateButtons();
  send('refresh');
</script></html>"""


def companion(method, endpoint, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", COMPANION_PORT, timeout=3)
    body = json.dumps(payload) if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(method, endpoint, body, headers)
        response = connection.getresponse()
        value = json.load(response)
        if not 200 <= response.status < 300:
            raise RuntimeError(value.get("message", f"K2 companion returned HTTP {response.status}."))
        return value
    finally:
        connection.close()


def scalar(value):
    return value[0] if isinstance(value, (list, tuple)) else value


def calibration_profile(preset, edited):
    if not preset.is_compatible:
        raise ValueError("Select a filament preset compatible with the active printer and nozzle.")
    if edited.name == preset.name and edited.is_dirty:
        raise ValueError(f"Save the changes to {preset.name} before calibrating.")

    def value(key):
        return scalar(preset.config_value(key))

    material = value("filament_type")
    temperature = round(float(value("nozzle_temperature")))
    diameter = float(value("filament_diameter"))
    current_flow = float(value("filament_max_volumetric_speed"))
    if material not in ("PETG", "ABS"):
        raise ValueError("Auto calibration supports PETG and ABS filament presets.")
    low, high = (value(key) for key in ("nozzle_temperature_range_low", "nozzle_temperature_range_high"))
    if low not in (None, "") and high not in (None, "") and not int(float(low)) <= temperature <= int(float(high)):
        raise ValueError(f"Set the nozzle temperature inside the filament's {int(float(low))}–{int(float(high))} °C range.")
    if not all(math.isfinite(v) and v > 0 for v in (diameter, current_flow)):
        raise ValueError("Set a valid filament diameter and maximum volumetric speed.")
    adaptive = value("adaptive_pressure_advance")
    return {"name": preset.name, "file": preset.file, "material": material,
            "temperature": temperature, "diameter": diameter, "flow": current_flow,
            "pa": value("pressure_advance"), "enabled": value("enable_pressure_advance"),
            "adaptive_model": value("adaptive_pressure_advance_model"),
            "adaptive": adaptive in (True, 1, "1", "true")}


def get_profile(bundle, name):
    preset = bundle.filaments.find_preset(name)
    if preset is None or preset.name != name or not preset.is_user() or not preset.file:
        raise ValueError("Select a saved user filament preset.")
    return calibration_profile(preset, bundle.filaments.edited_preset())


def list_profiles(bundle):
    collection = bundle.filaments
    edited = collection.edited_preset()
    assigned = [preset.name for preset in bundle.current_filament_presets() if preset is not None]
    profiles = []
    for index in range(collection.size()):
        preset = collection.preset(index)
        if not preset.is_user() or not preset.file or not preset.is_compatible:
            continue
        try:
            profile = calibration_profile(preset, edited)
        except ValueError as error:
            profiles.append({"name": preset.name, "issue": str(error)})
        except (TypeError, IndexError, OverflowError):
            profiles.append({"name": preset.name, "issue": "Check the saved temperature, diameter and max flow settings."})
        else:
            profiles.append({key: profile[key] for key in ("name", "material", "temperature", "adaptive")})
    profiles.sort(key=lambda p: p["name"].casefold())
    ready = [p["name"] for p in profiles if not p.get("issue")]
    default = next((name for name in assigned if name in ready), ready[0] if ready else None)
    return profiles, default


def wall_geometry(bundle, nozzle, wall):
    def config(key):
        return scalar(bundle.full_config_value(key))

    def width_value(value):
        if isinstance(value, str) and value.endswith("%"):
            return nozzle * float(value[:-1]) / 100
        if isinstance(value, dict):
            return float(value["value"]) * (nozzle / 100 if value["percent"] else 1)
        return float(value)

    width = width_value(config(f"{wall}_wall_line_width")) or width_value(config("line_width")) or 1.125 * nozzle
    layer, speed = float(config("layer_height")), float(config(f"{wall}_wall_speed"))
    area = layer * (width - layer * (1 - math.pi / 4))
    if not all(math.isfinite(v) for v in (width, layer, speed, area)) or area <= 0 or layer <= 0 or speed < 0:
        raise ValueError(f"Check {wall} wall width, speed and layer height in the process preset.")
    return area, speed


def wall_flow(bundle, nozzle, wall):
    area, speed = wall_geometry(bundle, nozzle, wall)
    return area * speed


def adaptive_walls(bundle, nozzle, ceiling):
    def config(key, fallback=None):
        value = scalar(bundle.full_config_value(key))
        if value is None:
            return fallback
        # Orca can serialize numeric vectors as text with the normal-mode limit first.
        if isinstance(value, str):
            value = value.split(",", 1)[0]
        return float(value)

    default = config("default_acceleration", 0.)
    if not math.isfinite(default) or default <= 0:
        raise ValueError("Set a positive normal printing acceleration in the active process preset.")
    cap = config("machine_max_acceleration_extruding") or float("inf")
    default_corner = config("default_jerk", 0.)
    walls = {}
    for wall in ("outer", "inner"):
        area, speed = wall_geometry(bundle, nozzle, wall)
        speed = min(speed or ceiling / area, ceiling / area)
        acceleration = config(f"{wall}_wall_acceleration") or default
        corner = (config(f"{wall}_wall_jerk", 0.) or default_corner) if default_corner else 0.
        walls[wall] = {"flow": min(speed * area, ceiling), "speed": speed,
                       "acceleration": min(acceleration, cap), "corner_velocity": corner}
    return walls


def coupon_settings(bundle, preset):
    plate = scalar(bundle.full_config_value("curr_bed_type"))
    if plate in (None, "", "Default Plate"):
        plate = scalar(bundle.current_printer_preset().config_value("default_bed_type")) or "Textured PEI Plate"
    if plate == "Default Plate":
        plate = "Textured PEI Plate"
    key = {"Textured PEI Plate": "textured_plate", "High Temp Plate": "hot_plate",
           "Engineering Plate": "eng_plate", "Cool Plate": "cool_plate",
           "Textured Cool Plate": "textured_cool_plate", "Supertack Plate": "supertack_plate"}.get(plate)
    if key is None:
        raise ValueError("Select a known bed type for the Adaptive PA coupon.")
    bed = scalar(preset.config_value(key + "_temp_initial_layer"))
    if bed is None:
        raise ValueError(f"Set a first-layer bed temperature for {plate} in the filament preset.")
    chamber = 0
    if scalar(preset.config_value("activate_chamber_temp_control")) in (True, 1, "1", "true"):
        value = scalar(preset.config_value("chamber_temperature"))
        if value is None:
            raise ValueError("Set a build chamber temperature in the selected filament preset.")
        chamber = int(str(value).split(",", 1)[0])
        if not 0 <= chamber <= 60:
            raise ValueError("Set the Adaptive PA filament chamber target to 0–60 °C.")
    return {"bed_temperature_c": int(str(bed).split(",", 1)[0]),
            "chamber_temperature_c": chamber,
            "layer_height_mm": float(scalar(bundle.full_config_value("layer_height"))),
            "first_layer_height_mm": float(scalar(bundle.full_config_value("initial_layer_print_height")))}


def selected_nozzle(bundle, printer=None):
    preset = bundle.current_printer_preset()
    if printer is not None:
        address = str(preset.config_value("print_host") or "")
        host = urlsplit(address if "://" in address else "//" + address).hostname
        if host != printer:
            raise ValueError(f"Select the Orca printer preset that connects to {printer}.")
    return float(scalar(preset.config_value("nozzle_diameter")))


def request_for(mode, profile, slot, printer, nozzle, outer, inner, *, walls=None):
    if mode not in MODES:
        raise ValueError("Choose a calibration.")
    if slot["material"] != profile["material"]:
        raise ValueError("The CFS material needs to match the filament preset.")
    if mode == "pressure_advance" and profile["adaptive"]:
        raise ValueError("Choose a filament preset with fixed pressure advance.")
    if mode in ("max_flow", "flow_then_pa") and not slot["max_flow"]:
        raise ValueError("Max Flow is not available for this CFS slot.")
    payload = {"kind": mode, "printer": printer, "slot": slot["slot"], "material": profile["material"],
               "temperature_c": profile["temperature"], "nozzle_mm": nozzle,
               "filament_diameter_mm": profile["diameter"], "max_flow_mm3_s": profile["flow"]}
    if mode == "pressure_advance":
        max_flow = profile["flow"]
        peak = min(outer or max_flow, max_flow)
        limit = slot.get("pa_peak_limit_mm3_s")
        if limit is None or peak > limit:
            allowed = f"up to {limit:g} mm³/s" if limit is not None else "no flow"
            raise ValueError(f"The outer wall requests {peak:g} mm³/s. Auto PA supports {allowed} "
                             f"at {profile['temperature']} °C for {profile['material']} in CFS {slot['slot']}.")
        payload.update(reader="continuous", max_flow_mm3_s=peak)
    elif mode in ("adaptive_pa", "flow_then_pa"):
        payload.update(walls=walls, pa_seed=float(profile["pa"]))
    return payload


def describe_adaptive(result):
    lines = []
    rows = []
    for point in result["points"]:
        label = f"{point['wall'].capitalize()} · {point['acceleration']:g} mm/s² · target {point['flow']:.4g} mm³/s"
        if point.get("observed_flow") is not None:
            label += f" · recorded {point['observed_flow']:.4g} mm³/s"
        if point["pa"] is None:
            lines.append(f"{label} · no bracketed PA")
            continue
        rise, fall = point["selected_scores"]
        lines.append(f"{label} · PA {point['pa']:.4f} · speedup {rise:.4f} · slowdown {fall:.4f}")
        rows.append(f"{point['pa']:.4f},{point['measured_flow']:.8g},{point['acceleration']:g}")
    changes = {}
    if result["complete_grid"]:
        changes = {"enable_pressure_advance": "1", "adaptive_pressure_advance": "1",
                   "adaptive_pressure_advance_model": json.dumps("\n".join(rows))}
        lines.extend(["", "Orca model rows (PA, flow, acceleration)", *rows])
    else:
        lines.append("A complete measurement grid is needed to apply a model.")
    if result.get("report_file"):
        lines.append(f"Adaptive PA report: {result['report_file']}")
    return lines, changes


def describe_result(mode, result, request):
    if mode == "adaptive_pa":
        lines, changes = describe_adaptive(result)
        return "\n".join(lines), changes
    lines, changes = [], {}
    flow = result.get("max_flow") if mode == "flow_then_pa" else result if mode == "max_flow" else None
    pa = result if mode == "pressure_advance" else None
    if flow is not None:
        cutoff = flow.get("recommended_mm3_s")
        if cutoff is None:
            if flow.get("reason") == "current_below_threshold":
                ceiling = flow["tested_through_mm3_s"]
                step = next(row for row in flow["steps"] if row["flow_mm3_s"] == ceiling)
                lines.append(f"Median current stayed below 1.0 through the {ceiling:g} mm³/s test ceiling "
                             f"({step['motor_median_abs_iq']:.3f} at the ceiling).")
            elif flow.get("reason") == "unbracketed_current":
                lines.append(f"Max Flow tested through {flow['tested_through_mm3_s']:g} mm³/s and ran out "
                             "of probes before verifying a pair around the 1.0 current cutoff.")
            else:
                lines.append("Max Flow found no verified pair around the 1.0 current cutoff.")
        else:
            lines.extend([f"Max volumetric speed: {cutoff:g} mm³/s",
                          f"{flow['below_cutoff_mm3_s']:g} mm³/s: median |Iq| {flow['below_cutoff_median_abs_iq']:.5g} (below 1.0)",
                          f"{cutoff:g} mm³/s: median |Iq| {flow['boundary_median_abs_iq']:.5g} (at or above 1.0)"])
            changes["filament_max_volumetric_speed"] = f"{cutoff:g}"
        if flow.get("capture"):
            lines.append(f"Flow report: {flow['capture']}")
    if mode == "flow_then_pa":
        adaptive = result.get("adaptive_pa")
        if adaptive is None:
            lines.append(result["pa_message"])
        else:
            adaptive_lines, adaptive_changes = describe_adaptive(adaptive)
            lines.extend(["", "Adaptive PA with wall flow capped at the measured Max Flow", *adaptive_lines])
            changes.update(adaptive_changes)
    elif pa is not None:
        lines.append(f"Outer-wall PA target: {request['max_flow_mm3_s']:.4g} mm³/s.")
    if pa is not None:
        candidate = pa.get("candidate")
        if candidate is None:
            lines.append("Auto PA found no eligible pressure advance value.")
        else:
            lines.append(f"Pressure advance: {candidate:.4f}")
            changes.update(pressure_advance=f"{candidate:.6g}", enable_pressure_advance="1")
            scores = pa.get("selected_scores")
            if scores and len(scores) == 2:
                lines.append(f"Outer-wall scores: speedup {scores[0]:.4f}, slowdown {scores[1]:.4f}")
            elif scores and len(scores) == 4:
                lines.append(f"Lower flow scores: speedup {scores[0]:.4f}, slowdown {scores[1]:.4f}")
                lines.append(f"Higher flow scores: speedup {scores[2]:.4f}, slowdown {scores[3]:.4f}")
        if pa.get("report_file"):
            lines.append(f"PA report: {pa['report_file']}")
    return "\n".join(lines), changes


class AutoCalibrations(orca.script.ScriptPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        self.state = {"profiles": [], "slots": [], "status": "", "busy": False}
        self.health = {}
        self.cancel = threading.Event()
        self.job_path = None
        self.apply_result = None
        self.window = None

    def get_name(self):
        return "Auto Calibrations"

    def execute(self):
        if self.window is None or not self.window.is_open():
            self.window = orca.host.ui.create_window(
                html=PAGE, title=self.get_name(), width=760, height=610,
                on_message=self.on_message, on_close=self.on_window_close,
            )
        return orca.ExecutionResult.success("Auto Calibrations opened.")

    def publish(self, **changes):
        self.state.update(changes)
        window = self.window
        if window is not None:
            window.post(dict(self.state))

    def launch(self, function, *args):
        try:
            threading.Thread(target=function, args=args, daemon=True).start()
        except (RuntimeError, OSError) as error:
            self.publish(busy=False, can_cancel=False, status=str(error))

    def refresh(self):
        bundle = orca.host.preset_bundle()
        nozzle = selected_nozzle(bundle)
        profiles, default = list_profiles(bundle)
        self.publish(busy=True, profiles=profiles, default_preset=default, nozzle=f"{nozzle:g}",
                     calibration_supported=False, status="Reading the K2 nozzle setting and CFS slots…")
        self.launch(self.read_slots, nozzle, bool(profiles))

    def read_slots(self, nozzle, has_profiles):
        try:
            health = companion("GET", "/health")
            if not health.get("combined_calibration"):
                raise RuntimeError("Restart the K2 companion with the combined-calibration update.")
            if not health.get("profile_temperature_range") or not health.get("max_flow_then_adaptive_pa"):
                raise RuntimeError("Restart the K2 companion with the Adaptive PA combination update.")
            if not health.get("standalone_pa_outer_wall_only"):
                raise RuntimeError("Restart the K2 companion with the outer-wall PA update.")
            sizes = health.get("supported_nozzles_mm")
            if not sizes:
                raise RuntimeError("Restart the K2 companion with the nozzle-detection update.")
            setup = companion("GET", "/slots")
            reported = setup["nozzle_mm"]
            matches = math.isclose(nozzle, reported, rel_tol=0, abs_tol=1e-6)
            supported = nozzle in sizes and matches
            if nozzle not in sizes:
                allowed = " or ".join(f"{size:g}" for size in sizes)
                status = f"Auto Calibrations supports {allowed} mm nozzles."
            elif not matches:
                status = (f"Orca selected {nozzle:g} mm, but the K2 reports {reported:g} mm. "
                          "Set the same diameter on the K2 and in Orca, then refresh.")
            elif not has_profiles:
                status = f"No saved filament presets are compatible with the active {nozzle:g} mm printer preset."
            else:
                status = "Select a filament preset and its matching CFS slot."
            self.health = health
            self.publish(busy=False, printer=health["printer"], slots=setup["slots"], status=status,
                         calibration_supported=supported, adaptive_supported=bool(
                             health.get("printed_adaptive_pa") and health.get("adaptive_pa_profile_preheat")))
        except ConnectionRefusedError:
            self.health = {}
            self.publish(busy=False, printer=None, slots=[], calibration_supported=False,
                         status=("The K2 companion is not running. Start the 'K2 Auto PA Companion' "
                                 "scheduled task (or run Start-K2-AutoPA-Orca.cmd), then refresh."))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            self.health = {}
            self.publish(busy=False, printer=None, slots=[], calibration_supported=False,
                         status=f"Could not read the K2. {error}")

    def start(self, mode, name, address):
        if not self.health:
            raise RuntimeError("Connect the K2 companion and refresh the slots.")
        slot = next((s for s in self.state["slots"] if s["slot"] == address), None)
        if slot is None:
            raise ValueError("Select a connected CFS slot.")
        bundle = orca.host.preset_bundle()
        profile = get_profile(bundle, name)
        nozzle = selected_nozzle(bundle, self.health["printer"])
        if (not self.state["calibration_supported"] or f"{nozzle:g}" != self.state["nozzle"]):
            raise ValueError("Refresh the presets and confirm that Orca and the K2 report the same nozzle diameter.")
        outer = wall_flow(bundle, nozzle, "outer") if mode == "pressure_advance" else 0.
        inner = 0.
        walls = None
        if mode == "adaptive_pa":
            walls = adaptive_walls(bundle, nozzle, profile["flow"])
        elif mode == "flow_then_pa":
            # The measured Max Flow caps these targets once step 1 finishes.
            walls = adaptive_walls(bundle, nozzle, slot["pa_peak_limit_mm3_s"])
        payload = request_for(mode, profile, slot, self.health["printer"], nozzle, outer, inner, walls=walls)
        text = (f"{MODES[mode]} · {profile['name']} · CFS {address} · {nozzle:g} mm nozzle\n\n"
                f"Clear the build plate and purge chute and stay by the printer. "
                f"The nozzle will heat to {profile['temperature']} °C and the toolhead will move.")
        if mode == "pressure_advance":
            text += f"\n\nOuter-wall PA target from the active process preset: {payload['max_flow_mm3_s']:.4g} mm³/s."
        elif mode in ("adaptive_pa", "flow_then_pa"):
            payload.update(coupon_settings(bundle, bundle.filaments.find_preset(name)))
            if mode == "flow_then_pa":
                text += ("\n\nStep 1 measures Max Flow. Step 2 then runs Adaptive PA with the wall flow targets "
                         "capped at the measured Max Flow. Keep the purge chute and build area clear throughout both tests.")
            text += (f"\n\nThe bed will heat to {payload['bed_temperature_c']} °C and soak for 3 minutes. "
                     "The printer will then probe around the tower and brim.")
            if payload["chamber_temperature_c"]:
                text += f"\nThe chamber will heat to {payload['chamber_temperature_c']} °C in the background."
            text += "\nFour combinations will record flow and pressure through the corners. Active wall settings"
            for wall, target in walls.items():
                text += (f"\n{wall.capitalize()} wall {target['flow']:.4g} mm³/s at "
                         f"{target['acceleration']:.4g} mm/s²")
            text += "\nWhen wall targets coincide, the pilot adds a slower flow or lower acceleration."
        if orca.host.ui.message(text, title=self.get_name(), buttons="yes_no", icon="question") != "yes":
            return
        self.cancel.clear()
        self.apply_result = None
        self.publish(busy=True, can_cancel=True, can_apply=False, report="", status="Starting calibration…")
        self.launch(self.run, payload, profile)

    def run(self, payload, profile):
        try:
            job = companion("POST", "/calibrations", payload)
            if not re.fullmatch("[a-f0-9]{32}", job["id"]):
                raise ValueError("The K2 companion returned an invalid calibration ID.")
            self.job_path = "/calibrations/" + job["id"]
            cancel_sent = False
            failed_polls = 0
            while job["state"] == "running":
                try:
                    if self.cancel.is_set() and not cancel_sent:
                        job = companion("DELETE", self.job_path)
                        cancel_sent = True
                    else:
                        job = companion("GET", self.job_path)
                    failed_polls = 0
                except (OSError, http.client.HTTPException):
                    failed_polls += 1
                    if failed_polls >= 3:
                        raise
                self.publish(can_cancel=not cancel_sent,
                             status="Canceling after the current printer operation…" if cancel_sent else job.get("message", "Measuring…"))
                if job["state"] == "running":
                    time.sleep(1)
            if job["state"] != "complete":
                self.publish(status=job.get("message", "Calibration ended."))
                return
            report, changes = describe_result(payload["kind"], job["result"], payload)
            nozzle = payload["nozzle_mm"]
            if changes:
                self.apply_result = (profile, changes, nozzle)
            self.publish(report=f"Filament preset: {profile['name']} · {nozzle:g} mm nozzle\n{report}",
                         status="Calibration finished.", can_apply=bool(changes))
        except (RuntimeError, OSError, ValueError, KeyError, http.client.HTTPException) as error:
            if self.job_path:
                try:
                    companion("DELETE", self.job_path)
                except (RuntimeError, OSError, ValueError, http.client.HTTPException):
                    pass
            self.publish(status=f"Calibration could not finish. {error}")
        finally:
            self.job_path = None
            self.publish(busy=False, can_cancel=False)

    def apply(self):
        profile, changes, nozzle = self.apply_result
        bundle = orca.host.preset_bundle()
        if not math.isclose(selected_nozzle(bundle), nozzle, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"Select the tested {nozzle:g} mm nozzle preset before applying this result.")
        if get_profile(bundle, profile["name"]) != profile:
            raise ValueError("The tested filament preset changed. Restore the tested preset before applying this result.")
        slot = next((number for number, assigned in enumerate(bundle.current_filament_presets(), 1)
                     if assigned is not None and assigned.name == profile["name"]), None)
        if slot is None:
            raise ValueError(f"Assign {profile['name']} to a filament in Prepare first, then click Apply again.")
        self.publish(busy=True, status="Applying the result to the Orca filament editor…")
        self.launch(self.apply_changes, slot, profile, changes)

    def apply_changes(self, slot, profile, changes):
        try:
            transaction = orca.host.preset.filament.begin_update(slot)
            if transaction.target_name != profile["name"]:
                raise RuntimeError("The filament assigned in Orca changed.")
            for key, value in changes.items():
                transaction.set(key, value)
            transaction.commit()
            self.apply_result = None
            self.publish(status=f"Applied to {profile['name']}. Save the filament preset in Orca to keep it.",
                         can_apply=False)
        except (RuntimeError, ValueError) as error:
            self.publish(status=str(error))
        finally:
            self.publish(busy=False)

    def on_message(self, message):
        if not isinstance(message, dict):
            return
        action = message.get("type")
        if action == "cancel" and self.state.get("can_cancel"):
            self.cancel.set()
            self.publish(can_cancel=False, status="Canceling after the current printer operation…")
            return
        if self.state["busy"]:
            self.publish()
            return
        try:
            if action == "refresh":
                self.refresh()
            elif action == "start":
                self.start(message["mode"], message["preset"], message["slot"])
            elif action == "apply" and self.apply_result:
                self.apply()
        except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as error:
            self.publish(status=str(error))

    def on_window_close(self):
        self.window = None
        self.cancel.set()

    def on_unload(self):
        self.cancel.set()
        if self.job_path:
            try:
                companion("DELETE", self.job_path)
            except (RuntimeError, OSError, ValueError, http.client.HTTPException):
                pass


@orca.plugin
class K2CalibrationPlugin(orca.base):
    def register_capabilities(self):
        orca.request_permissions(network=[f"http://127.0.0.1:{COMPANION_PORT}"], threading=True)
        orca.register_capability(AutoCalibrations)
