CTFd.plugin.run((_CTFd) => {
  const $ = _CTFd.lib.$;
  const md = _CTFd.lib.markdown();

  // Disable flag modal popup
  window.challenge = window.challenge || {};
  window.challenge.data = window.challenge.data || {};
  window.challenge.data.flags = [];

  // Parse flag pattern and auto-fill hidden fields
  function parseFlagPattern() {
    const patternInput = document.getElementById("flag_pattern");
    const preview = document.getElementById("flag_pattern_preview");
    if (!patternInput || !preview) return;

    const pattern = patternInput.value;
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

  // Auto-set internal_port from compose YAML expose field
  function updateInternalPort() {
    var textarea = document.getElementById("compose_config");
    var hidden = document.getElementById("internal_port_hidden");
    if (!textarea || !hidden) return;

    var match = textarea.value.match(/expose:\s*(\d+)/);
    if (match) {
      hidden.value = match[1];
    }
  }

  // Initialize
  (function () {
    const flagPatternInput = document.getElementById("flag_pattern");
    if (flagPatternInput) {
      flagPatternInput.addEventListener("input", parseFlagPattern);
      try {
        parseFlagPattern();
      } catch (e) {}
    }

    const composeConfig = document.getElementById("compose_config");
    if (composeConfig) {
      composeConfig.addEventListener("input", updateInternalPort);
      updateInternalPort();
    }

    // Scoring type toggle
    const scoringTypeSelect = document.getElementById("scoring_type");
    if (scoringTypeSelect) {
      scoringTypeSelect.addEventListener("change", function () {
        const scoringType = this.value;
        const standardSection = document.getElementById("standard-scoring");
        const dynamicSection = document.getElementById("dynamic-scoring");
        if (!standardSection || !dynamicSection) return;

        if (scoringType === "standard") {
          standardSection.style.display = "block";
          dynamicSection.style.display = "none";
          const sv = document.getElementById("standard_value");
          if (sv) {
            sv.required = true;
            sv.disabled = false;
          }
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
          const sv = document.getElementById("standard_value");
          if (sv) {
            sv.required = false;
            sv.disabled = true;
          }
          [
            { id: "dynamic_initial", req: true },
            { id: "dynamic_decay", req: true },
            { id: "dynamic_minimum", req: true },
            { id: "decay_function", req: false },
          ].forEach((f) => {
            const el = document.getElementById(f.id);
            if (el) {
              el.required = f.req;
              el.disabled = false;
            }
          });
        }
      });
      scoringTypeSelect.dispatchEvent(new Event("change"));
    }
  })();
});
