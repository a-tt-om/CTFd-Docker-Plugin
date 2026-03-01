CTFd.plugin.run((_CTFd) => {
  const $ = _CTFd.lib.$;
  const md = _CTFd.lib.markdown();

  window.challenge = window.challenge || {};
  window.challenge.data = window.challenge.data || {};
  window.challenge.data.flags = [];
});

// Parse flag pattern
function parseFlagPattern() {
  const pattern = document.getElementById("flag_pattern").value;
  const preview = document.getElementById("flag_pattern_preview");
  const randomMatch = pattern.match(/<ran_(\d+)>/);

  if (randomMatch) {
    const randomLength = parseInt(randomMatch[1]);
    const parts = pattern.split(randomMatch[0]);
    document.getElementById("flag_mode").value = "random";
    document.getElementById("flag_prefix").value = parts[0] || "";
    document.getElementById("flag_suffix").value = parts[1] || "";
    document.getElementById("random_flag_length").value = randomLength;
    const exampleRandom = "x".repeat(randomLength);
    preview.innerHTML = `✓ Random mode: <code>${parts[0]}${exampleRandom}${parts[1]}</code> (${randomLength} random chars)`;
    preview.style.color = "#17a2b8";
  } else {
    document.getElementById("flag_mode").value = "static";
    document.getElementById("flag_prefix").value = pattern;
    document.getElementById("flag_suffix").value = "";
    document.getElementById("random_flag_length").value = 0;
    preview.innerHTML = `✓ Static mode: <code>${pattern}</code> (same for all teams)`;
    preview.style.color = "#28a745";
  }
}

document.addEventListener("DOMContentLoaded", function () {
  const flagPatternInput = document.getElementById("flag_pattern");
  if (flagPatternInput) {
    flagPatternInput.addEventListener("input", parseFlagPattern);
    setTimeout(parseFlagPattern, 100);
  }
});

// Scoring toggle
document.getElementById("scoring_type").addEventListener("change", function () {
  const scoringType = this.value;
  const standardSection = document.getElementById("standard-scoring");
  const dynamicSection = document.getElementById("dynamic-scoring");

  if (scoringType === "standard") {
    standardSection.style.display = "block";
    dynamicSection.style.display = "none";
    document.getElementById("standard_value").required = true;
    document.getElementById("standard_value").disabled = false;
    [
      "dynamic_initial",
      "dynamic_decay",
      "dynamic_minimum",
      "decay_function",
    ].forEach((id) => {
      const f = document.getElementById(id);
      if (f) {
        f.required = false;
        f.disabled = true;
      }
    });
  } else {
    standardSection.style.display = "none";
    dynamicSection.style.display = "block";
    document.getElementById("standard_value").required = false;
    document.getElementById("standard_value").disabled = true;
    ["dynamic_initial", "dynamic_decay", "dynamic_minimum"].forEach((id) => {
      const f = document.getElementById(id);
      if (f) {
        f.required = true;
        f.disabled = false;
      }
    });
    const df = document.getElementById("decay_function");
    if (df) {
      df.required = false;
      df.disabled = false;
    }
  }
});

// Set connection type from challenge data
var connectType = document.getElementById("connect-type");
if (connectType && typeof container_connection_type_selected !== "undefined") {
  connectType.value = container_connection_type_selected;
}
