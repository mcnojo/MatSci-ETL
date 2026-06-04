# ocr-benchmarking - BEWARE: Work in Progress
Testing baseline, local, and sota OCR methods on a difficult set of chemical/material science IP/Papers, specifically for sodium-ion battery systems, starting with electrolyte design. Why sodium ion electrolyte design?
- https://en.highstar.com/blog/energy-density-sodium-vs-lithium-battery-comparison-analysis
- simulating electrolyte molecules is feasible on minmal hardware, minutes not hours for full DFT

Potential problem sets:
1 - Hard Carbon SEI engienering
**2 - Ether Oxidative Stability Extension** <- first anyhow
3 - Salt Anion Design
4 - Low Temp Electrolyte Design
5 - First Cycle Efficiency / ICE


## Models of interest

### OCR
- https://huggingface.co/datalab-to/chandra-ocr-2
- https://huggingface.co/deepseek-ai/DeepSeek-OCR-2
- https://huggingface.co/rednote-hilab/dots.mocr
- https://huggingface.co/allenai/olmOCR-2-7B-1025

### Reasoners (may need to benchmark on relevant tasks, re retrosynthesis)
- https://huggingface.co/osunlp/LlaSMol-Mistral-7B
- https://huggingface.co/weidawang/Chem-R-8B

### Plot/chart data extraction
- https://github.com/automeris-io/WebPlotDigitizer
- https://huggingface.co/google/deplot

## Resources:
- [ParseBench leaderboard](https://huggingface.co/datasets/llamaindex/ParseBench?eval_result=infly/Infinity-Parser2-Pro&leaderboard_task_id=chart)
- [Materials Project APIs/tools](https://docs.materialsproject.org/)
- [pymatgen](https://pymatgen.org/)
- Framework which sits at top of the [chembench leaderboard](https://huggingface.co/spaces/jablonkagroup/ChemBench-Leaderboard), [Nexus Sci Agent](https://github.com/CASIA-LM/S1-NexusAgent)
- [Hackathon submissions](https://llmhackathon.github.io/submissions/) for chem, goldmine of architecures to integrate


## Data
(umbrella catagories and specific sources searched with gpt)

Layered oxide cathodes:
Paper:
- LixCoO 2 (0<x~l): A NEW CATHODE MATERIAL FOR BATTERIES OF HIGH ENERGY DENSITY 
    - https://sci-hub.ru/10.1016/0025-5408(80)90012-4
    - (https://www.sciencedirect.com/science/article/abs/pii/0025540880900124)
Patent: 
- Fast ion conductors
    - https://patents.google.com/patent/US4357215A/en?oq=US4357215A


LFP / olivine phosphate cathodes:
Paper:
- Phytic acid derived LiFePO4 beyond theoretical capacity as high-energy density cathode for lithium ion battery
    - https://sci-hub.ru/10.1016/j.nanoen.2017.03.006
    - (https://www.sciencedirect.com/science/article/abs/pii/S2211285517301374)

Patent:
- Cathode materials for secondary (rechargeable) lithium batteries
    - https://patents.google.com/patent/US6514640B1/en?oq=US6514640B1


SEI formation on anodes:
Paper:
- The Electrochemical Behavior of Alkali and Alkaline Earth Metals in Nonaqueous Battery Systems—The Solid Electrolyte Interphase Model
    - https://sci-hub.ru/10.1149/1.2128859
    - (https://iopscience.iop.org/article/10.1149/1.2128859/pdf)

Patent:
- An additive for lithium ion rechargeable battery cells
    - https://patents.google.com/patent/EP2430686B1/en?oq=EP2430686B1


Graphite/carbon anodes:
Paper:
- A reversible graphite-lithium negative electrode for electrochemical generators
    - https://sci-hub.ru/10.1016/0378-7753(83)87040-2
    - (https://www.sciencedirect.com/science/article/abs/pii/0378775383870402?via%3Dihub)

Patent:
- Process for producing carbon anode compositions for lithium ion batteries
    - https://patents.google.com/patent/US7993780B2/en?oq=US7993780


Electrochemical impedance spectroscopy — EIS
Paper:
- Application of electrochemical impedance spectroscopy to commercial Li-ion cells: A review
    - https://www.sciencedirect.com/science/article/pii/S0378775320310466

Patent: (this is more of a method so no specific patent on this)


Silicon Anodes
Paper:
- Fundamental Investigation of Silicon Anode
in Lithium-Ion Cells
    - https://ntrs.nasa.gov/api/citations/20120016539/downloads/20120016539.pdf

Patent:
- Nanostructured silicon for battery anodes
    - https://patents.google.com/patent/US8791449B2/en?oq=US8791449B2d


Garnet LLZO solid electrolytes
Paper:
- Degradation Mechanism of All-Solid-State Li-Metal Batteries Studied by Electrochemical Impedance Spectroscopy
    - https://pmc.ncbi.nlm.nih.gov/articles/PMC9478940/

Patent:
- Lithium stuffed garnet setter plates for solid electrolyte fabrication
    - https://patents.google.com/patent/US9970711B2/en?oq=US9970711B2


Sulfide/solid-state battery interfaces
Patent:
- Solid-state battery
    - https://patents.google.com/patent/US20210167417A1/en?oq=US20210167417A1


Electrolyte additives / high-voltage stability
Paper:
- Vinylene carbonate and vinylene trithiocarbonate as electrolyte additives for lithium ion battery
    - https://sci-hub.ru/10.1016/j.jpowsour.2011.06.058
    - https://www.sciencedirect.com/science/article/abs/pii/S0378775311012948

Patent:
- Lithium ion battery electrolyte additive
    - https://patents.google.com/patent/US20220109187A1/en?oq=US20220109187A1


Battery degradation / diagnostics
Paper:
- Electrochemical Impedance Spectroscopy as a Diagnostic and Prognostic Tool for EV Batteries: A Review
    - https://www.jecst.org/upload/pdf/jecst-2024-01060.pdf




## Specific selections
Selecting a smaller set of specific pages to test with, hand picked for varience in:
- Infomation density
- Figure type
- Resolution
- Descriptive clarity


Paper Selections:
data/papers/1-s2.0-S0378775320310466-main.pdf - pg 9
data/papers/20120016539.pdf - pg 13
data/papers/am2c09841.pdf - pg 5
data/papers/chang2011.pdf - pg 6 
data/papers/LiFePO4_zhao2017.pdf - pg 417 (10)
data/papers/LixCoO2_mizushima1980.pdf - pgs 3, 6
data/papers/SEI_model_peled1979.pdf - pg 2
data/papers/yazami1983.pdf - pg 6

Patent Selections:
data/patents/An_additive_for_lithium_ion_rechargeable_battery_cells_EP2430686B1.pdf - pgs 22, 38
data/patents/Cathode_materials_for_secondary_(rechargeable)_lithium_batteriesUS6514640.pdf - pg 7 ---- IMPORTANT
data/patents/fast_ion_conductors_US4357215.pdf - pgs 1, 3
data/patents/US7993780.pdf - 3
data/patents/US8791449.pdf - 6
data/patents/US9970711.pdf - 22
data/patents/US20210167417A1.pdf - 16 (textually dense)
data/patents/US20220109187A1.pdf - 10, 11 (checmical)


## Layout Preprocessing Stage
- try a few methods for cleanly separating textual content from figures, tables, graphs etc.

Methods:
- OpenCV morphological
- OpenCV projecction
- DocLayout-YOLO

## Notes on each:

### Doclayout-Yolo
- Best overall at apprpriate bounds/text
- data/pages/1-s2.0-S0378775320310466-main/9/doclayout_yolo/figures/figure_0.png should instead be further chunked (possibly a post-parse check then further split via openCV?)
- Text sometimes extracted twice over: data/pages/chang2011/6/doclayout_yolo/text/text_4.png / text_5.png
- Tables / figures merged, creating duplicate sets, will need to estimate via pizel ranges what is unique data/pages/yazami1983/6/doclayout_yolo/figures

### OpenCV Morphological
- actually better at dense figures: data/pages/1-s2.0-S0378775320310466-main/9/doclayout_yolo/figures/figure_0.png 
- mistakes sometext heavy figures for text: data/pages/1-s2.0-S0378775320310466-main/9/opencv_morphological/text/text_9.png
- axes cut off data/pages/An_additive_for_lithium_ion_rechargeable_battery_cells_EP2430686B1/38/opencv_projection/figures/figure_1.png
- Dense and old? ie data/pages/SEI_model_peled1979
    - need a lot of disambiguation/context per figure, else are too abstract for OCR model (presumably)
- Micharacterization of figures with nearby formula as formula data/pages/SEI_model_peled1979/2/doclayout_yolo/figures/isolate_formula_8.png

### OpenCV projection
- real bad, frequently mistakes figures for text
- critical axes cut off data/pages/An_additive_for_lithium_ion_rechargeable_battery_cells_EP2430686B1/38/opencv_projection/figures/figure_1.png


## Notes / Improvements to Evaluate:
- Test efficacy of OCR for dense figures ie, data/pages/LiFePO4_zhao2017/10/doclayout_yolo/figures/figure_0.png
    - may need adiditonal split
    - may need a way to associate description with OCR for dense figure (context wise)
- May want to trim noisy / partial artifacts ie: data/pages/LixCoO2_mizushima1980/6/doclayout_yolo/figures/table_0.png
    - Check text metadat position, reconstruct missing/partial artifact and fill in as rectangular?
- Interpretation of schematics via OCR model a domain to be explored, ie data/pages/US7993780/3/doclayout_yolo/figures/figure_0.png
- Tables / figures merged, creating duplicate sets, will need to estimate via pizel ranges what is unique data/pages/yazami1983/6/doclayout_yolo/figures
- No idea how to scientific high res images ie data/pages/US8791449/6/page_6.png
    - contextual identification -> ...?  Might not get a lot of value if simply an image, but the concept ought to be stored from its description.


# Figure Remediation Methods:

## Core Features:
We want to correct associate the figure/subfigure metadata (ie: a. through e.) with the respective graph.
    > solution: something either heursitic based or model based...

## Edge Cases:
Figure has some text which is cut off - detected by 'ink' at the edge of the image
    > solution: find whatever other bounds overlap within that vicinity and stitch then together, filling in whitespace where needed.


### Pre Pipeline composition...
host on aws olmo + rednote + deepseek OCR behand an api gate so that we can test directly the data we get out of each respectively.

> ./launch.sh deepseek/olmo/etc
> ssh -i /Users/noah/.ssh/deepseek-ocr-key.pem ubuntu@( IP ) 'tail -f ~/vllm_serve.log'
> cd ..
> python batch.py

### Combinations:
1) across each model for a subset of images
(report and iterate)

Looks like chandra is the way to go, i mean ParseBench speaks volumes with respect to charts/tables, but it's still not perfect with respect to extracting *all* data form the tables.

We need to test these combos:

0. Pipeline to correctly segment dense images and contents in papers.
    > this is to be done first

1. Just Chandra
2. Just VL
3. Chandra + VL



### Potential improvements:
- preprocess large *sets* of tables into composed sets of figures
- optimize 


## Paper Processing Output Shape:
Formalize a local file system for papers -> text/figures/hierarchy
    - a dictionary/tree of metadata containing sectionwise contents and references to file paths and their OCR status.
        - (includes position metadata to detail what was on the same page, which caption refers to what, etc during prompt construction)
    - a few tools for file open/read
    - a scratchpad to refer to with notes regarding the paper.


4/25/26
trying to do a kind of local scientific paper page index to generate a doc/file reference epresentation of each paper

lotta context manipulation with ollama, should probably standardize to vllm once I have a sense of how the context and max token work

notably, attention is quadratic in sequence length... gotta batch/overlap carefully

echo 'FROM gemma3:4b                                                 
  PARAMETER num_ctx 8192' > /tmp/Modelfile.gemma4b
ollama create gemma3:4b-8k -f /tmp/Modelfile.gemma4b      



## Part 2: 
- Minimal harness and local interaction
- Run on two laptops linked by ports..
- Goal: (determine in precise language a testable [simulatable] and searchable molecule)
- Iterative harness between search (arxiv, materials project)
    - first pass comprehension
    - OCR and result formalization
    - storage
    - TRACK LATENCY HERE AND FORMALLY NOTE ITERATIVE IMPROVEMENTS

- Once a number of papers have results formalized and summarized, propose hypotheses to test/simulate in a sandbox
    - first pass with a model which approximates reults
    - second is the formal simulation



## Noted on Simualtion:
- Atomate appears to contain a set of valuable simulators: https://atomate.org/
- anything by https://github.com/hackingmaterials
- 