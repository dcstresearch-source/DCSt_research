import time
import csv
import math
import os
from collections import deque

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator
from qiskit.circuit import Parameter

# -----------------------------
# CONFIGURATION
# -----------------------------
XPLANE_FILENAME = "D_Flight_1.txt"
PLAYBACK_SPEED = 0

# Aircraft reference parameters (matching modern classical system)
AIRCRAFT_WEIGHT_LBS = 3400.0                # Reference aircraft weight in pounds
V_STALL_1G_REF = 45.0                       # 1G stall speed at reference weight (clean config)
WING_AREA_SQ_FT = 174.0                     # Wing reference area
RHO_SEA_LEVEL = 0.002377                    # Air density at sea level (slugs/ft³)

# Quantum simulation parameters
SHOTS = 128                                 # Number of quantum measurements per evaluation
EMA_ALPHA = 0.25                           # Exponential moving average smoothing factor
USE_SMOOTHING = True                        # Enable EMA smoothing to reduce quantum noise

# Ice and configuration detection thresholds
ICE_SPEED_PENALTY_KT = 10.0                # Speed penalty when icing detected
FLAPS_VSTALL_REDUCTION = 0.85              # Flaps reduce stall speed by ~15%

# Turbulence estimation window
TURBULENCE_WINDOW = 10                      # Number of samples for turbulence estimation

# Column preferences matching classical system
SPEED_CANDIDATES = ["_Vind,_kias", "Vtrue,_ktas", "Vtrue,_ktgs"]
AOA_CANDIDATES = ["alpha,__deg"]
ALT_CANDIDATES = ["p-alt,ftMSL", "___CG,ftMSL", "terrn,ftMSL"]
TIME_CANDIDATES = ["_totl,_time"]

# -----------------------------
# QUANTUM SIMULATOR
# -----------------------------
simulator = AerSimulator()

# -----------------------------
# QUANTUM CIRCUIT TEMPLATE (PARAMETRIZED)
# Enhanced with configuration awareness and sensor fusion
# -----------------------------
# Primary parameters
theta_aoa_p = Parameter("theta_aoa")
theta_speed_p = Parameter("theta_speed")
theta_turb_p = Parameter("theta_turb")
theta_config_p = Parameter("theta_config")

# Secondary AoA sensor for redundancy
theta_aoa2_p = Parameter("theta_aoa2")

# Create 6-qubit circuit for enhanced stall detection
# Qubit 0: Primary AoA sensor
# Qubit 1: Speed (IAS)
# Qubit 2: Turbulence/environmental
# Qubit 3: Configuration state (flaps/ice)
# Qubit 4: Secondary AoA sensor (redundancy)
# Qubit 5: Risk assessment output
qc_template = QuantumCircuit(6, 1)

# ===== STEP 1: ENCODE INPUT PARAMETERS =====
# Encode primary AoA sensor
qc_template.ry(theta_aoa_p, 0)

# Encode airspeed
qc_template.ry(theta_speed_p, 1)

# Encode turbulence level
qc_template.ry(theta_turb_p, 2)

# Encode configuration state (flaps/ice)
qc_template.ry(theta_config_p, 3)

# Encode secondary AoA sensor
qc_template.ry(theta_aoa2_p, 4)

# ===== STEP 2: QUANTUM SENSOR FUSION =====
# Entangle primary and secondary AoA sensors
# If sensors agree, coherence is maintained; if they disagree, decoherence increases
qc_template.cz(0, 4)  # Controlled-Z creates correlation between sensors
qc_template.cry(math.pi / 4, 0, 4)  # Conditional rotation based on primary sensor

# ===== STEP 3: CROSS-PARAMETER ENTANGLEMENT =====
# Create quantum correlations between flight parameters
# These represent physical dependencies in stall physics

# AoA-Speed correlation (primary stall physics)
qc_template.cp(math.pi / 2, 0, 5)
qc_template.cry(math.pi / 1.5, 0, 5)

# Speed contribution to risk
qc_template.cry(math.pi / 2, 1, 5)

# Turbulence amplification of risk (turbulence makes stall more likely)
qc_template.ccx(0, 2, 5)  # Toffoli: high AoA AND turbulence increases risk

# Configuration influence (flaps/ice modify stall characteristics)
qc_template.cry(math.pi / 3, 3, 5)

# ===== STEP 4: SENSOR DISAGREEMENT HANDLING =====
# If sensors disagree significantly, apply additional uncertainty
# This creates a "cautious" quantum state that biases toward higher risk
qc_template.cx(4, 5)  # Secondary sensor contributes to final risk

# ===== STEP 5: MEASUREMENT =====
# Measure risk qubit (qubit 5) onto classical bit
qc_template.measure(5, 0)

# Transpile once for efficiency
qc_transpiled = transpile(qc_template, simulator)

# -----------------------------
# UTILITY FUNCTIONS
# -----------------------------
def clip(x, lo, hi):
    """Clamp value to range [lo, hi]"""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x

# -----------------------------
# DYNAMIC STALL SPEED CALCULATION
# Modern system feature: calculate Vs based on actual flight conditions
# -----------------------------
def calculate_dynamic_vstall(
    weight_lbs: float,
    load_factor: float,
    altitude_ft: float,
    icing_detected: bool = False,
    flaps_deployed: bool = False
) -> float:
    """
    Calculate dynamic stall speed based on actual flight conditions.
    
    This mirrors the modern classical system to ensure fair comparison.
    
    Physics:
    - Vs proportional to sqrt(Weight)
    - Vs proportional to sqrt(Load factor)
    - Vs proportional to 1/sqrt(Air density)
    
    Args:
        weight_lbs: Current aircraft weight
        load_factor: Load factor (1.0 = level flight, >1.0 = turning/maneuvering)
        altitude_ft: Pressure altitude in feet MSL
        icing_detected: Whether ice contamination detected
        flaps_deployed: Whether flaps are extended
    
    Returns:
        Dynamic stall speed in knots
    """
    # Weight correction: Vs ~ sqrt(W/W_ref)
    weight_ratio = weight_lbs / AIRCRAFT_WEIGHT_LBS
    weight_factor = math.sqrt(max(0.5, min(2.0, weight_ratio)))
    
    # Load factor correction: Vs ~ sqrt(n)
    load_factor = max(1.0, load_factor)
    load_factor_multiplier = math.sqrt(load_factor)
    
    # Altitude/density correction: Vs ~ 1/sqrt(rho/rho_0)
    # Simplified standard atmosphere model
    density_ratio = math.exp(-altitude_ft / 30000.0)
    density_factor = 1.0 / math.sqrt(max(0.3, density_ratio))
    
    # Configuration factor
    config_factor = 1.0
    if flaps_deployed:
        config_factor = FLAPS_VSTALL_REDUCTION
    
    # Calculate base dynamic stall speed
    vs_dynamic = V_STALL_1G_REF * weight_factor * load_factor_multiplier * density_factor * config_factor
    
    # Ice protection penalty (ice increases stall speed)
    if icing_detected:
        vs_dynamic += ICE_SPEED_PENALTY_KT
    
    return vs_dynamic

# -----------------------------
# TURBULENCE ESTIMATION
# Derive turbulence from data variability instead of using fixed value
# -----------------------------
def estimate_turbulence_from_data(aoa_history: deque, speed_history: deque) -> float:
    """
    Estimate turbulence level from variability in AoA and speed.
    
    Real aircraft encounter turbulence that manifests as rapid fluctuations
    in angle of attack and airspeed. By analyzing variance in recent data,
    we can estimate turbulence intensity.
    
    Args:
        aoa_history: Recent AoA values (degrees)
        speed_history: Recent speed values (knots)
    
    Returns:
        Turbulence level in [0, 1] where 0=calm, 1=severe
    """
    if len(aoa_history) < 3 or len(speed_history) < 3:
        return 0.0  # Not enough data, assume calm
    
    # Calculate variance in AoA (primary turbulence indicator)
    aoa_list = list(aoa_history)
    aoa_mean = sum(aoa_list) / len(aoa_list)
    aoa_variance = sum((x - aoa_mean) ** 2 for x in aoa_list) / len(aoa_list)
    
    # Calculate variance in speed (secondary indicator)
    speed_list = list(speed_history)
    speed_mean = sum(speed_list) / len(speed_list)
    speed_variance = sum((x - speed_mean) ** 2 for x in speed_list) / len(speed_list)
    
    # Normalize variances to [0, 1] range
    # Typical AoA variance in calm air: <0.5 deg², in moderate turbulence: 1-3 deg²
    # Typical speed variance in calm air: <1 kt², in moderate turbulence: 3-10 kt²
    aoa_turb = min(1.0, aoa_variance / 3.0)
    speed_turb = min(1.0, speed_variance / 10.0)
    
    # Combine (AoA turbulence weighted higher)
    turbulence_level = 0.7 * aoa_turb + 0.3 * speed_turb
    
    return clip(turbulence_level, 0.0, 1.0)

# -----------------------------
# CONFIGURATION STATE DETECTION
# Infer flaps/ice state from flight parameters
# -----------------------------
def detect_configuration_state(speed_kts: float, vs_dynamic: float, aoa_deg: float) -> tuple[bool, bool, float]:
    """
    Infer aircraft configuration state from flight parameters.
    
    In real systems, flap position and ice detection come from sensors.
    For this simulation, we infer from flight characteristics:
    - Flaps: Flying slow with high AoA but stable (approach configuration)
    - Ice: Speed margin deteriorating without obvious cause
    
    Args:
        speed_kts: Current airspeed
        vs_dynamic: Calculated dynamic stall speed
        aoa_deg: Current angle of attack
    
    Returns:
        (flaps_deployed, icing_detected, config_state_value)
    """
    flaps_deployed = False
    icing_detected = False
    config_state = 0.0
    
    # Flaps inference: low speed (1.2-1.4 Vs) with moderate AoA (5-10°)
    if vs_dynamic > 0:
        speed_ratio = speed_kts / vs_dynamic
        if 1.2 <= speed_ratio <= 1.5 and 5.0 <= aoa_deg <= 12.0:
            flaps_deployed = True
            config_state = 0.5
    
    # Ice inference: very low speed margin without flaps explanation
    # (In real system this comes from ice detector)
    if speed_kts < vs_dynamic + 15.0 and not flaps_deployed and aoa_deg > 8.0:
        icing_detected = True
        config_state = 1.0
    
    return flaps_deployed, icing_detected, config_state

# -----------------------------
# QUANTUM ENCODING FUNCTIONS
# Map flight parameters to quantum rotation angles
# -----------------------------
def speed_to_theta(speed_kts: float, vs_dynamic: float) -> float:
    """
    Map airspeed to quantum rotation angle [0, π].
    
    Uses dynamic stall speed for normalization (not fixed reference).
    Lower speed margin = larger rotation = higher risk contribution.
    
    Mapping:
    - Speed >= 2.0*Vs → θ = 0 (no rotation, safe state |0⟩)
    - Speed = Vs → θ = π (full rotation to |1⟩, stall state)
    - Linear interpolation between
    
    Args:
        speed_kts: Current indicated airspeed
        vs_dynamic: Current dynamic stall speed
    
    Returns:
        Rotation angle in radians [0, π]
    """
    if vs_dynamic <= 1e-6 or speed_kts <= 0.0:
        return math.pi  # Invalid state, assume high risk
    
    ratio = speed_kts / vs_dynamic
    
    if ratio < 1.0:
        return math.pi  # Below stall speed
    if ratio > 2.0:
        return 0.0  # Well above stall, safe
    
    # Linear mapping: ratio in [1.0, 2.0] → theta in [π, 0]
    return (2.0 - ratio) * math.pi

def aoa_to_theta(aoa_deg: float, aoa_critical: float = 16.0) -> float:
    """
    Map angle of attack to quantum rotation angle [0, π].
    
    Higher AoA = larger rotation = higher risk contribution.
    
    Mapping:
    - AoA = 0° → θ = 0 (safe state)
    - AoA = critical AoA → θ = π (stall state)
    - Linear interpolation
    
    Args:
        aoa_deg: Current angle of attack in degrees
        aoa_critical: Critical AoA for stall (aircraft-specific)
    
    Returns:
        Rotation angle in radians [0, π]
    """
    aoa_clipped = clip(float(aoa_deg), 0.0, aoa_critical)
    return (aoa_clipped / aoa_critical) * math.pi

def turbulence_to_theta(turb_level: float) -> float:
    """
    Map turbulence level to quantum rotation angle [0, π/2].
    
    Turbulence amplifies stall risk by making flight less stable.
    We use smaller rotation range to make it a modifier rather than primary input.
    
    Args:
        turb_level: Turbulence level [0, 1]
    
    Returns:
        Rotation angle in radians [0, π/2]
    """
    turb_clipped = clip(turb_level, 0.0, 1.0)
    return turb_clipped * (math.pi / 2.0)

def config_to_theta(config_state: float) -> float:
    """
    Map configuration state to quantum rotation angle [0, π/2].
    
    Configuration (flaps/ice) modifies stall characteristics.
    
    Args:
        config_state: 0.0=clean, 0.5=flaps, 1.0=ice
    
    Returns:
        Rotation angle in radians [0, π/2]
    """
    config_clipped = clip(config_state, 0.0, 1.0)
    return config_clipped * (math.pi / 2.0)

# -----------------------------
# DUAL SENSOR QUANTUM FUSION
# Quantum approach to sensor redundancy
# -----------------------------
def calculate_sensor_disagreement_angle(aoa1_deg: float, aoa2_deg: float) -> float:
    """
    Calculate quantum phase angle representing sensor disagreement.
    
    In classical systems, sensors are compared and faults flagged when
    disagreement exceeds threshold. In quantum approach, disagreement
    is encoded as phase shift that affects measurement probability.
    
    Large disagreement → larger phase shift → more uncertain state
    
    Args:
        aoa1_deg: Primary AoA sensor reading
        aoa2_deg: Secondary AoA sensor reading
    
    Returns:
        Phase angle in radians representing disagreement magnitude
    """
    disagreement = abs(aoa1_deg - aoa2_deg)
    
    # Map disagreement to phase angle [0, π/4]
    # Small disagreement (<1°) → minimal phase shift
    # Large disagreement (>5°) → maximum phase shift
    if disagreement < 1.0:
        return 0.0
    elif disagreement > 5.0:
        return math.pi / 4
    else:
        return (disagreement - 1.0) / 4.0 * (math.pi / 4)

# -----------------------------
# QUANTUM RISK EVALUATION
# Core DCSt algorithm
# -----------------------------
def evaluate_quantum_stall_risk(
    speed_kts: float,
    aoa_deg: float,
    weight_lbs: float,
    load_factor: float,
    altitude_ft: float,
    turbulence_level: float,
    aoa_sensor2_deg: float = None
) -> tuple[float, dict]:
    """
    DCSt (Quantum Stall Detection) core algorithm.
    
    Evaluates stall risk using quantum circuit with:
    - Dynamic stall speed calculation
    - Dual-sensor quantum fusion
    - Configuration awareness
    - Turbulence adaptation
    
    This is the main quantum advantage: all parameters are entangled
    in superposition, allowing the circuit to evaluate complex
    interdependencies that would require extensive classical logic.
    
    Args:
        speed_kts: Indicated airspeed (knots)
        aoa_deg: Primary angle of attack sensor (degrees)
        weight_lbs: Aircraft weight
        load_factor: Load factor (1.0 = level flight)
        altitude_ft: Pressure altitude (feet MSL)
        turbulence_level: Estimated turbulence [0, 1]
        aoa_sensor2_deg: Secondary AoA sensor (for redundancy)
    
    Returns:
        (risk_probability, debug_info_dict)
        - risk_probability: Measured probability of stall state [0, 1]
        - debug_info: Dictionary with intermediate calculations
    """
    # Step 1: Calculate dynamic stall speed (modern system feature)
    flaps_deployed, icing_detected, config_state = detect_configuration_state(
        speed_kts, V_STALL_1G_REF, aoa_deg  # Use reference for initial detection
    )
    
    vs_dynamic = calculate_dynamic_vstall(
        weight_lbs, load_factor, altitude_ft, icing_detected, flaps_deployed
    )
    
    # Step 2: Encode parameters as quantum rotation angles
    theta_speed = speed_to_theta(speed_kts, vs_dynamic)
    theta_aoa = aoa_to_theta(aoa_deg, aoa_critical=16.0)
    theta_turb = turbulence_to_theta(turbulence_level)
    theta_config = config_to_theta(config_state)
    
    # Step 3: Handle dual-sensor input
    if aoa_sensor2_deg is not None:
        theta_aoa2 = aoa_to_theta(aoa_sensor2_deg, aoa_critical=16.0)
        # In real implementation, disagreement would modify circuit parameters
        # For now, we encode secondary sensor directly
    else:
        # If no second sensor, duplicate primary (perfect redundancy simulation)
        theta_aoa2 = theta_aoa
    
    # Step 4: Bind parameters to quantum circuit
    bound_circuit = qc_transpiled.assign_parameters(
        {
            theta_aoa_p: theta_aoa,
            theta_speed_p: theta_speed,
            theta_turb_p: theta_turb,
            theta_config_p: theta_config,
            theta_aoa2_p: theta_aoa2,
        },
        inplace=False,
    )
    
    # Step 5: Execute quantum circuit
    result = simulator.run(bound_circuit, shots=SHOTS).result()
    counts = result.get_counts()
    
    # Step 6: Extract risk probability from measurement
    ones = counts.get("1", 0)
    risk_probability = ones / float(SHOTS)
    
    # Step 7: Compile debug information
    debug_info = {
        "vs_dynamic_kt": vs_dynamic,
        "speed_margin_kt": speed_kts - vs_dynamic,
        "theta_speed_rad": theta_speed,
        "theta_aoa_rad": theta_aoa,
        "theta_turb_rad": theta_turb,
        "theta_config_rad": theta_config,
        "flaps_deployed": flaps_deployed,
        "icing_detected": icing_detected,
        "turbulence_level": turbulence_level,
        "load_factor": load_factor,
        "quantum_counts": counts,
    }
    
    return risk_probability, debug_info

# -----------------------------
# FILE PARSING UTILITIES
# -----------------------------
def parse_header_line(header_line: str):
    """Extract column names from X-Plane header line"""
    parts = [p.strip() for p in header_line.strip().split("|")]
    return [p for p in parts if p]

def parse_data_line(data_line: str):
    """Parse data line and convert to floats"""
    raw = [p.strip() for p in data_line.strip().split("|")]
    raw = [p for p in raw if p]
    values = []
    for item in raw:
        try:
            values.append(float(item))
        except ValueError:
            return None
    return values

def pick_column_index(columns, candidates):
    """Find first matching column from candidates list"""
    for name in candidates:
        if name in columns:
            return columns.index(name), name
    return None, None

# -----------------------------
# MAIN SIMULATION
# -----------------------------
if not os.path.exists(XPLANE_FILENAME):
    print(f"ERROR: Could not find '{XPLANE_FILENAME}'")
    raise SystemExit(1)

output_filename = f"dcst_results_{int(time.time())}.csv"

print("=" * 80)
print("DCSt - QUANTUM STALL DETECTION SYSTEM")
print("Enhanced with Modern Flight Dynamics")
print("=" * 80)
print(f"Input file: {XPLANE_FILENAME}")
print(f"Output file: {output_filename}")
print()
print("Quantum Configuration:")
print(f"  Circuit qubits: 6 (AoA₁, Speed, Turbulence, Config, AoA₂, Risk)")
print(f"  Measurement shots: {SHOTS}")
print(f"  Smoothing (EMA α): {EMA_ALPHA if USE_SMOOTHING else 'Disabled'}")
print()
print("Aircraft Parameters:")
print(f"  Reference weight: {AIRCRAFT_WEIGHT_LBS:.0f} lbs")
print(f"  1G stall speed: {V_STALL_1G_REF:.1f} kts")
print(f"  Ice penalty: +{ICE_SPEED_PENALTY_KT:.1f} kts")
print(f"  Flaps reduction: {(1-FLAPS_VSTALL_REDUCTION)*100:.0f}%")
print("=" * 80)
print()

# Read flight data file
with open(XPLANE_FILENAME, "r", errors="ignore") as f:
    lines = f.readlines()

# Find header line
header_index = None
for i, line in enumerate(lines):
    if "|" in line and "_Vind,_kias" in line:
        header_index = i
        break

if header_index is None:
    print("ERROR: Could not find header line.")
    raise SystemExit(1)

columns = parse_header_line(lines[header_index])

# Identify required columns
speed_idx, speed_name = pick_column_index(columns, SPEED_CANDIDATES)
aoa_idx, aoa_name = pick_column_index(columns, AOA_CANDIDATES)
alt_idx, alt_name = pick_column_index(columns, ALT_CANDIDATES)
time_idx, time_name = pick_column_index(columns, TIME_CANDIDATES)

if None in (speed_idx, aoa_idx, alt_idx, time_idx):
    print("ERROR: Missing required columns.")
    print("Found:", columns)
    raise SystemExit(1)

print("Detected columns:")
print(f"  Time : {time_name} (index {time_idx})")
print(f"  Speed: {speed_name} (index {speed_idx})")
print(f"  AoA  : {aoa_name} (index {aoa_idx})")
print(f"  Alt  : {alt_name} (index {alt_idx})")
print()

data_start = header_index + 1

# State variables
risk_ema = None
processed = 0
skipped_empty = 0
skipped_parse = 0
skipped_no_time = 0
filled_forward = 0

# Tracking for statistics
t_first_written = None
t_last_written = None
t_last_seen = None

# Last known values for forward-fill
last_speed_kts = None
last_aoa_deg = None
last_alt_ft = None

# History buffers for turbulence estimation
aoa_history = deque(maxlen=TURBULENCE_WINDOW)
speed_history = deque(maxlen=TURBULENCE_WINDOW)

# Simulated flight state (in real system, from sensors)
current_weight = AIRCRAFT_WEIGHT_LBS
load_factor = 1.0  # Assume level flight (would come from accelerometer)

# Open output CSV
with open(output_filename, mode="w", newline="") as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow([
        "Step", "Time_sec", "Altitude_ft", "Speed_kts", "AoA_deg",
        "Vs_dynamic", "Turbulence", "Load_Factor", "Quantum_Risk_Raw",
        "Quantum_Risk_Smoothed", "Config_State"
    ])
    
    print("Processing flight data...")
    print("-" * 80)
    
    for step, line in enumerate(lines[data_start:], start=0):
        line = line.strip()
        if not line:
            skipped_empty += 1
            continue
        
        values = parse_data_line(line)
        if values is None:
            skipped_parse += 1
            continue
        
        # Require time column
        if len(values) <= time_idx:
            skipped_no_time += 1
            continue
        
        t_sec = float(values[time_idx])
        t_last_seen = t_sec
        
        # Extract available data (with forward-fill for missing columns)
        if speed_idx < len(values):
            last_speed_kts = float(values[speed_idx])
        
        if aoa_idx < len(values):
            last_aoa_deg = float(values[aoa_idx])
        
        if alt_idx < len(values):
            last_alt_ft = float(values[alt_idx])
        
        # Wait until we have initial state
        if (last_speed_kts is None) or (last_aoa_deg is None) or (last_alt_ft is None):
            continue
        
        # Track forward-fill usage
        if (speed_idx >= len(values)) or (aoa_idx >= len(values)) or (alt_idx >= len(values)):
            filled_forward += 1
        
        # Update history buffers
        aoa_history.append(last_aoa_deg)
        speed_history.append(last_speed_kts)
        
        # Estimate turbulence from data
        turbulence_level = estimate_turbulence_from_data(aoa_history, speed_history)
        
        # Simulate second AoA sensor (in real system, from physical sensor)
        # Add small noise to simulate realistic sensor differences
        aoa_sensor2 = last_aoa_deg + 0.15  # Within tolerance
        
        # Execute quantum stall detection
        risk_raw, debug_info = evaluate_quantum_stall_risk(
            speed_kts=last_speed_kts,
            aoa_deg=last_aoa_deg,
            weight_lbs=current_weight,
            load_factor=load_factor,
            altitude_ft=last_alt_ft,
            turbulence_level=turbulence_level,
            aoa_sensor2_deg=aoa_sensor2
        )
        
        # Apply EMA smoothing to reduce quantum measurement noise
        if USE_SMOOTHING:
            if risk_ema is None:
                risk_ema = risk_raw
            else:
                risk_ema = (EMA_ALPHA * risk_raw) + ((1.0 - EMA_ALPHA) * risk_ema)
            risk_output = risk_ema
        else:
            risk_output = risk_raw
        
        # Track time range
        if t_first_written is None:
            t_first_written = t_sec
        t_last_written = t_sec
        
        # Console output (sample to avoid spam)
        if step % 10 == 0 or risk_output > 0.6:
            status = "STALL" if risk_output > 0.8 else "WARNING" if risk_output > 0.5 else "NORMAL"
            print(
                f"t={t_sec:7.2f}s | Alt={last_alt_ft:7.0f}ft | IAS={last_speed_kts:5.1f}kt | "
                f"AoA={last_aoa_deg:5.2f}° | Vs={debug_info['vs_dynamic_kt']:5.1f}kt | "
                f"Turb={turbulence_level:4.2f} | QRisk={risk_output:5.3f} | {status}"
            )
        
        # Write to CSV
        writer.writerow([
            step,
            t_sec,
            last_alt_ft,
            last_speed_kts,
            last_aoa_deg,
            debug_info['vs_dynamic_kt'],
            turbulence_level,
            load_factor,
            risk_raw,
            risk_output,
            debug_info.get('flaps_deployed', 0) * 0.5 + debug_info.get('icing_detected', 0) * 0.5
        ])
        
        processed += 1
        
        if PLAYBACK_SPEED > 0:
            time.sleep(PLAYBACK_SPEED)

print("-" * 80)
print()
print("Simulation complete!")
print(f"  Processed data points: {processed}")
print(f"  Skipped (empty): {skipped_empty}")
print(f"  Skipped (parse error): {skipped_parse}")
print(f"  Skipped (no time): {skipped_no_time}")
print(f"  Forward-fill rows: {filled_forward}")
print()
print(f"Time range:")
print(f"  First written: {t_first_written:.2f}s")
print(f"  Last seen: {t_last_seen:.2f}s")
print(f"  Last written: {t_last_written:.2f}s")
print()
print(f"Results saved to: {output_filename}")
print()
print("Output columns:")
print("  Vs_dynamic: Dynamic stall speed (adaptive, like modern systems)")
print("  Turbulence: Estimated from data variance")
print("  Quantum_Risk_Raw: Direct quantum measurement")
print("  Quantum_Risk_Smoothed: EMA-filtered for stability")
print("  Config_State: Inferred configuration (0=clean, 0.5=flaps, 1.0=ice)")
