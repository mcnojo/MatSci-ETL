Problem 1 — Ether Oxidative Stability Extension
This is your most tractable problem end-to-end. The entire research loop — generate candidates, screen computationally, rank, propose — runs locally without needing a single cloud job for the core pipeline.
The oxidative stability of a solvent molecule is governed primarily by its HOMO energy (or more precisely, its ionization potential / oxidation potential in solution). Higher HOMO = easier to oxidize = lower oxidative stability. This is a single-molecule property. The simulation is: optimize geometry (xTB, ~seconds), compute HOMO with implicit solvation model (ORCA with SMD or CPCM solvation, ~5–15 minutes per molecule).
What you can actually run locally:

HOMO/oxidation potential of ether candidates — ✅ 5–15 min/molecule in ORCA
Fluorination effect screening (replace H with F at various positions, recompute HOMO) — ✅ systematic, automatable
Na⁺ solvation energy of candidate molecules (binding energy in the first coordination shell) — ✅ small clusters 3–6 molecules + Na⁺, tractable
xTB pre-screening of thousands of structural analogues before sending shortlist to ORCA — ✅ seconds per molecule
Classical MD ionic conductivity estimates of candidate solvent mixtures — ✅ LAMMPS with existing force fields

The full agentic loop is feasible: literature -> identify known ethers and measured oxidation potentials -> train a calibration relationship between computed HOMO and experimental oxidation potential -> generate fluorinated/modified ether analogues with RDKit -> xTB pre-screen -> ORCA shortlist -> rank -> propose candidates above 4.2V threshold with good Na⁺ solvation. This is a complete, publishable methodology.
Verdict: Fully tractable locally. This is your primary target.


alt/ next:

Problem 2 — Low Temperature Electrolyte Design
Tractable for transport properties, harder for interface effects.
Low temperature performance fails for two reasons: the bulk electrolyte becomes too viscous and ion transport slows, and the SEI repair mechanism at the anode fails. The bulk transport part is entirely classical MD — compute diffusion coefficients and viscosity of candidate electrolyte mixtures at different temperatures using LAMMPS. This is exactly what LAMMPS is designed for, runs on 10,000-atom periodic simulation boxes, and is well within your hardware.
The SEI repair failure at -50°C is harder — it's an interface process that requires either reactive MD (ReaxFF, computationally expensive but possible) or metadynamics/umbrella sampling to compute activation barriers for Na⁺ desolvation through a model SEI. These are feasible but long-running jobs (hours to a day per state point).
What you can actually run locally:

Diffusion coefficient D(Na⁺) as a function of temperature in candidate solvents — ✅ LAMMPS classical MD, hours per run
Viscosity as a function of temperature (Green-Kubo method) — ✅ LAMMPS, computationally intensive but feasible
Activation energy for ion diffusion (Arrhenius fit across temperatures) — ✅ run MD at 5 temperatures, fit
Desolvation energy of Na⁺ from candidate solvents (proxy for low-T kinetics) — ✅ small cluster DFT in ORCA

Verdict: Bulk transport properties are fully tractable in LAMMPS. Interface kinetics need more careful setup but are feasible for simple model systems.