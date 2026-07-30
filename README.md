# EEW Monitor
> This software was built with Codex GPT-5.6 sol

This is an [EEW](https://en.wikipedia.org/wiki/Earthquake_Early_Warning_(Japan)) (Earthquake Early Warning) software that pops warning info on your desktop (Windows) once an EEW message announces.

## Functions

- Receives EEW updates with WebSocket
  - Currently from [JMA](https://www.data.jma.go.jp/multi/quake/index.html) (Japan Meteorological Agency, 気象庁) only (credit to [Wolfx API](https://wolfx.jp/apidoc_en)).
  - Planning to support: [CENC](https://data.earthquake.cn/) (China Earthquake Networks Center, 国家地震科学数据中心) and more.
- Once received alert updates
  - Record files in json format, same file will be updated if the alert has been updated;
  - Short info messages printed in the terminal;
  - Pop-up window at the corner on Windows system.
- Compatibility
  - Only works for Windows OS, only tested on Win11.

## Requirements

- Python 3.9+
- packages
  - `websockets==15.0.1`
  - `win11toast==0.36.3`
