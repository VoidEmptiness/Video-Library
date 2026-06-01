function truncateText(value, limit = 20) {
  const text = value || "";
  if (text.length <= limit) return text;
  return `${text.slice(0, limit).trimEnd()}...`;
}

function collectTagFilters() {
  const checks = document.querySelectorAll('input[type="checkbox"][name="tag"]:checked');
  return Array.from(checks).map((checkbox) => checkbox.value).join(",");
}

function isUntaggedFilterEnabled(form) {
  return Boolean(form.querySelector("[data-untagged-filter]")?.checked);
}

function wireSearchForm() {
  const form = document.querySelector('form[action="/"]');
  if (!form) return;

  form.addEventListener("submit", (event) => {
    const ids = collectTagFilters();
    const untagged = isUntaggedFilterEnabled(form);
    const url = new URL(window.location.href);
    const query = form.querySelector('input[name="q"]')?.value || "";

    url.pathname = "/";
    if (query.trim()) url.searchParams.set("q", query);
    else url.searchParams.delete("q");

    if (untagged) {
      url.searchParams.set("untagged", "1");
      url.searchParams.delete("tags");
    } else {
      url.searchParams.delete("untagged");
      if (ids) url.searchParams.set("tags", ids);
      else url.searchParams.delete("tags");
    }

    window.location.href = url.toString();
    event.preventDefault();
  });
}

function wireUntaggedFilter() {
  const checkbox = document.querySelector("[data-untagged-filter]");
  if (!checkbox) return;

  const tagChecks = Array.from(document.querySelectorAll('input[type="checkbox"][name="tag"]'));

  function syncTagState() {
    for (const tagCheck of tagChecks) {
      tagCheck.disabled = checkbox.checked;
      if (checkbox.checked) tagCheck.checked = false;
    }
  }

  checkbox.addEventListener("change", syncTagState);
  syncTagState();
}

function wireBulkActions() {
  const form = document.querySelector("[data-bulk-delete-form]");
  const toggle = document.querySelector("[data-select-toggle]");
  const bulkBar = form?.querySelector("[data-bulk-bar]");
  const count = form?.querySelector("[data-selected-count]");
  const deleteButton = form?.querySelector("[data-delete-selected]");
  const selectAll = form?.querySelector("[data-select-all]");
  const selectClear = form?.querySelector("[data-select-clear]");
  const checks = Array.from(form?.querySelectorAll("[data-video-select]") || []);
  const bulkTagsBtn = document.querySelector("[data-bulk-tags-btn]");
  const modal = document.querySelector("[data-bulk-tags-modal]");
  const modalInfo = modal?.querySelector("[data-bulk-tags-info]");
  const tagChecks = Array.from(modal?.querySelectorAll("[data-bulk-tag-cb]") || []);
  const applyBtn = modal?.querySelector("[data-bulk-tags-apply]");
  const cancelBtn = modal?.querySelector("[data-bulk-tags-cancel]");
  const hiddenForm = modal?.querySelector("[data-bulk-tags-form]");
  const search = modal?.querySelector("[data-bulk-tag-search]");
  const tagList = modal?.querySelector("[data-bulk-tag-list]");
  const empty = modal?.querySelector("[data-bulk-tag-empty]");
  if (!form) return;

  let selectMode = false;

  function selectedCount() {
    return checks.filter((check) => check.checked).length;
  }

  function syncBulkUi() {
    const total = selectedCount();
    form.classList.toggle("select-mode", selectMode);
    if (bulkBar) bulkBar.hidden = !selectMode;
    if (toggle) toggle.textContent = selectMode ? "Готово" : "Выбрать";
    if (count) count.textContent = `${total} выбрано`;
    if (deleteButton) deleteButton.disabled = total === 0;
    if (bulkTagsBtn) bulkTagsBtn.disabled = total === 0;
  }

  toggle?.addEventListener("click", () => {
    selectMode = !selectMode;
    if (!selectMode) {
      for (const check of checks) check.checked = false;
    }
    syncBulkUi();
  });

  selectAll?.addEventListener("click", () => {
    for (const check of checks) check.checked = true;
    syncBulkUi();
  });

  selectClear?.addEventListener("click", () => {
    for (const check of checks) check.checked = false;
    syncBulkUi();
  });

  for (const check of checks) {
    check.addEventListener("change", syncBulkUi);
  }

  form.addEventListener("click", (event) => {
    if (!selectMode) return;
    const link = event.target.closest(".video-link");
    if (!link) return;
    event.preventDefault();
    const card = link.closest("[data-select-card]");
    const check = card?.querySelector("[data-video-select]");
    if (!check) return;
    check.checked = !check.checked;
    syncBulkUi();
  });

  form.addEventListener("submit", (event) => {
    const total = selectedCount();
    if (total === 0) {
      event.preventDefault();
      return;
    }
    if (!confirm(`Удалить выбранные видео: ${total}?`)) {
      event.preventDefault();
    }
  });

  if (bulkTagsBtn && modal) {
    function selectedVideoIds() {
      return checks.filter((c) => c.checked).map((c) => c.value);
    }

    function selectedTagValues() {
      return tagChecks.filter((c) => c.checked).map((c) => c.value);
    }

    function syncApplyBtn() {
      applyBtn.disabled = selectedTagValues().length === 0;
    }

    bulkTagsBtn.addEventListener("click", () => {
      modalInfo.textContent = `${selectedVideoIds().length} видео`;
      modal.hidden = false;
      syncApplyBtn();
    });

    for (const cb of tagChecks) {
      cb.addEventListener("change", syncApplyBtn);
    }

    cancelBtn?.addEventListener("click", () => {
      modal.hidden = true;
    });

    modal.addEventListener("click", (event) => {
      if (event.target === modal) {
        modal.hidden = true;
      }
    });

    applyBtn?.addEventListener("click", () => {
      const ids = selectedVideoIds();
      const tagVals = selectedTagValues();
      if (ids.length === 0 || tagVals.length === 0) return;

      hiddenForm.querySelectorAll("input[type=hidden]").forEach((el) => el.remove());

      const returnTo = form.querySelector('input[name="return_to"]');
      if (returnTo) {
        const rt = document.createElement("input");
        rt.type = "hidden";
        rt.name = "return_to";
        rt.value = returnTo.value;
        hiddenForm.appendChild(rt);
      }

      for (const vid of ids) {
        const inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = "video_ids";
        inp.value = vid;
        hiddenForm.appendChild(inp);
      }

      for (const tid of tagVals) {
        const inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = "tag_ids";
        inp.value = tid;
        hiddenForm.appendChild(inp);
      }

      hiddenForm.submit();
    });

    if (search && tagList) {
      const tags = Array.from(tagList.querySelectorAll("[data-bulk-tag-name]"));
      if (tags.length > 0) {
        function applyFilter() {
          const query = normalizeTagQuery(search.value);
          let visibleCount = 0;
          for (const tag of tags) {
            const tagName = tag.dataset.bulkTagName || "";
            const label = tag.textContent || "";
            const isVisible = !query || tagName.includes(query) || normalizeTagQuery(label).includes(query);
            tag.hidden = !isVisible;
            if (isVisible) visibleCount += 1;
          }
          if (empty) empty.hidden = visibleCount !== 0;
        }
        search.addEventListener("input", applyFilter);
        applyFilter();
      }
    }
  }

  syncBulkUi();
}

function wireSimpleSelect() {
  const roots = Array.from(document.querySelectorAll("[data-simple-select-root]"));
  for (const root of roots) {
    const toggle = root.querySelector("[data-simple-select-toggle]");
    const bar = root.querySelector("[data-simple-select-bar]");
    const count = root.querySelector("[data-simple-select-count]");
    const submit = root.querySelector("[data-simple-select-submit]");
    const checks = Array.from(root.querySelectorAll("[data-simple-select-item]"));
    if (!toggle || checks.length === 0) continue;

    let selectMode = false;
    const selectedCount = () => checks.filter((check) => check.checked).length;

    const sync = () => {
      const total = selectedCount();
      root.classList.toggle("select-mode", selectMode);
      if (bar) bar.hidden = !selectMode;
      toggle.textContent = selectMode ? "Готово" : "Выбрать";
      if (count) count.textContent = `${total} выбрано`;
      if (submit) submit.disabled = total === 0;
    };

    toggle.addEventListener("click", () => {
      selectMode = !selectMode;
      if (!selectMode) {
        for (const check of checks) check.checked = false;
      }
      sync();
    });

    for (const check of checks) check.addEventListener("change", sync);
    sync();
  }
}

function setUploadStatus(text) {
  const status = document.getElementById("uploadStatus");
  if (status) status.textContent = text;
}

function setUploadProgress(percent) {
  const progress = document.querySelector("[data-upload-progress]");
  const bar = document.querySelector("[data-upload-progress-bar]");
  if (!progress || !bar) return;
  progress.hidden = false;
  bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function resetUploadProgress() {
  const progress = document.querySelector("[data-upload-progress]");
  const bar = document.querySelector("[data-upload-progress-bar]");
  if (bar) bar.style.width = "0%";
  if (progress) progress.hidden = true;
}

function uploadFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return Promise.resolve();

  setUploadProgress(0);
  setUploadStatus(`Загрузка: 0% (${list.length} файл.)`);

  const formData = new FormData();
  for (const file of list) formData.append("files", file);

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/videos/upload", true);
    request.withCredentials = true;

    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) {
        setUploadStatus(`Загрузка файлов: ${list.length}`);
        return;
      }

      const percent = Math.round((event.loaded / event.total) * 100);
      setUploadProgress(percent);
      setUploadStatus(`Загрузка: ${percent}%`);

      if (percent >= 100) {
        setUploadStatus("Файл отправлен. Завершаю загрузку...");
      }
    });

    request.addEventListener("load", () => {
      if (request.status < 200 || request.status >= 400) {
        reject(new Error(request.responseText || `HTTP ${request.status}`));
        return;
      }

      setUploadProgress(100);
      setUploadStatus("Готово. Обновляю страницу...");
      window.location.href = "/";
      resolve();
    });

    request.addEventListener("error", () => reject(new Error("Не удалось загрузить файл")));
    request.addEventListener("abort", () => reject(new Error("Загрузка отменена")));
    request.send(formData);
  });
}

function wireDropzone() {
  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("fileInput");
  const form = document.getElementById("uploadForm");
  const chooseButton = document.getElementById("chooseBtn");
  const fileLabel = document.getElementById("fileLabel");
  if (!dropzone || !input || !form) return;

  function updateFileLabel() {
    const files = input.files;
    if (!fileLabel) return;

    if (!files || files.length === 0) {
      fileLabel.textContent = "Файлы не выбраны";
      fileLabel.removeAttribute("title");
      resetUploadProgress();
    } else if (files.length === 1) {
      fileLabel.textContent = truncateText(files[0].name);
      fileLabel.title = files[0].name;
    } else {
      fileLabel.textContent = `Выбрано файлов: ${files.length}`;
      fileLabel.title = Array.from(files).map((file) => file.name).join("\n");
    }
  }

  chooseButton?.addEventListener("click", () => input.click());
  input.addEventListener("change", updateFileLabel);
  updateFileLabel();

  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });

  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));

  dropzone.addEventListener("drop", async (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragover");

    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) return;

    try {
      await uploadFiles(files);
    } catch (error) {
      alert(`Ошибка загрузки: ${error}`);
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const files = input.files;
    if (!files || files.length === 0) return;

    try {
      await uploadFiles(files);
    } catch (error) {
      const message = String(error || "");
      if (message.includes("Failed to fetch") || message.includes("TypeError")) {
        setUploadStatus("Не удалось загрузить через JavaScript. Отправляю обычной формой...");
        form.submit();
        return;
      }
      alert(`Ошибка загрузки: ${error}`);
    }
  });
}

function normalizeTagQuery(value) {
  return value.trim().toLocaleLowerCase();
}

function getCurrentTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}

function setTheme(theme) {
  if (theme !== "dark" && theme !== "light") return;
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem("theme", theme);
  } catch (e) {}
}

function wireThemeToggle() {
  const buttons = Array.from(document.querySelectorAll("[data-theme-set]"));
  if (buttons.length === 0) return;

  function syncActive() {
    const theme = getCurrentTheme();
    for (const button of buttons) {
      const isActive = button.dataset.themeSet === theme;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
      const shouldShow = theme === "dark" ? button.dataset.themeSet === "light" : button.dataset.themeSet === "dark";
      button.hidden = !shouldShow;
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", () => {
      const next = button.dataset.themeSet;
      if (next !== "dark" && next !== "light") return;
      setTheme(next);
      syncActive();
    });
  }

  syncActive();
}

function formatDurationLabel(totalSeconds) {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
  const total = Math.floor(totalSeconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function wireVideoMeta() {
  const video = document.querySelector("video.player");
  const durationNode = document.querySelector("[data-video-duration]");
  const resolutionNode = document.querySelector("[data-video-resolution]");
  if (!video || !durationNode) return;

  video.style.aspectRatio = "16 / 9";

  video.addEventListener("error", () => {
    let msg = "Не удалось воспроизвести видео";
    if (video.error) {
      if (video.error.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
        msg += ". Возможно, кодек не поддерживается браузером (например, HEVC/H.265).";
      } else {
        msg += ` (код: ${video.error.code})`;
      }
    }
    durationNode.textContent = msg;
    if (resolutionNode) resolutionNode.textContent = "";
  });

  const updateDuration = () => {
    durationNode.textContent = `Длительность: ${formatDurationLabel(video.duration)}`;
  };

  const resolveVideoDimensions = () => {
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (w > 0 && h > 0) {
      if (resolutionNode) resolutionNode.textContent = `Разрешение: ${w}×${h}`;
      video.style.aspectRatio = `${w} / ${h}`;
      return true;
    }
    return false;
  };

  const pollDimensions = () => {
    if (resolveVideoDimensions()) return;
    let attempts = 0;
    const interval = setInterval(() => {
      attempts++;
      if (resolveVideoDimensions() || attempts > 50) clearInterval(interval);
    }, 100);
  };

  const onLoadStart = () => {
    updateDuration();
    pollDimensions();
  };

  video.addEventListener("loadedmetadata", onLoadStart);
  video.addEventListener("loadeddata", pollDimensions);
  video.addEventListener("durationchange", updateDuration);
  video.addEventListener("resize", pollDimensions);
  video.addEventListener("playing", pollDimensions);
  if (video.readyState >= 1) onLoadStart();
  if (video.readyState >= 2) pollDimensions();
  if (document.readyState === "complete") pollDimensions();
  window.addEventListener("load", pollDimensions);
  video.addEventListener("progress", pollDimensions);
}

function isTypingTarget(target) {
  if (!target) return false;
  const el = target;
  const tag = (el.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || el.isContentEditable;
}

function wireVideoPlayerShortcuts() {
  const video = document.querySelector("video.player");
  if (!video) return;

  let fpsEstimate = 30;
  let sampleCount = 0;
  let lastMediaTime = null;

  function tryEstimateFps() {
    if (!("requestVideoFrameCallback" in HTMLVideoElement.prototype)) return;
    if (sampleCount >= 20) return;
    video.requestVideoFrameCallback((now, metadata) => {
      if (typeof metadata?.mediaTime === "number") {
        if (lastMediaTime != null) {
          const delta = metadata.mediaTime - lastMediaTime;
          if (delta > 0 && delta < 0.25) {
            const candidate = 1 / delta;
            if (candidate > 10 && candidate < 120) {
              fpsEstimate = Math.round((fpsEstimate * 3 + candidate) / 4);
              sampleCount += 1;
            }
          }
        }
        lastMediaTime = metadata.mediaTime;
      }
      if (!video.paused && !video.ended) tryEstimateFps();
    });
  }

  video.addEventListener("play", () => {
    sampleCount = 0;
    lastMediaTime = null;
    tryEstimateFps();
  });

  function seekBy(seconds) {
    const next = Math.max(0, Math.min(video.duration || Infinity, video.currentTime + seconds));
    video.currentTime = next;
  }

  async function stepFrame(direction) {
    const wasPaused = video.paused;
    if (!wasPaused) video.pause();

    const step = 1 / (fpsEstimate || 30);
    seekBy(step * direction);

    await new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        video.removeEventListener("seeked", finish);
        resolve();
      };
      video.addEventListener("seeked", finish, { once: true });
      setTimeout(finish, 250);
    });

    if (!wasPaused) video.play().catch(() => {});
  }

  document.addEventListener("keydown", (event) => {
    if (isTypingTarget(event.target)) return;
    if (event.altKey || event.metaKey || event.ctrlKey) return;

    if (event.key === "ArrowLeft") {
      seekBy(event.shiftKey ? -10 : -5);
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowRight") {
      seekBy(event.shiftKey ? 10 : 5);
      event.preventDefault();
      return;
    }

    if (event.key === "," || event.key === "<") {
      stepFrame(-1);
      event.preventDefault();
      return;
    }
    if (event.key === "." || event.key === ">") {
      stepFrame(1);
      event.preventDefault();
      return;
    }
  });
}

function wireFolderToggles() {
  const toggles = document.querySelectorAll("[data-folder-toggle]");
  for (const toggle of toggles) {
    toggle.addEventListener("click", () => {
      const card = toggle.closest(".folder-card");
      if (!card) return;
      const children = card.nextElementSibling;
      if (!children || !children.hasAttribute("data-folder-children")) return;
      const isHidden = children.hidden;
      children.hidden = !isHidden;
      toggle.classList.toggle("open", isHidden);
      toggle.setAttribute("aria-label", isHidden ? "Свернуть" : "Раскрыть");
    });
  }
}

function wireTagSearch() {
  const searches = document.querySelectorAll("[data-tag-search]");
  for (const search of searches) {
    const form = search.closest("form");
    if (!form) continue;

    const tagList = form.querySelector("[data-tag-list]");
    const empty = form.querySelector("[data-tag-empty]");
    const tags = Array.from(tagList?.querySelectorAll("[data-tag-name]") || []);
    if (!tagList || tags.length === 0) continue;

    function applyFilter() {
      const query = normalizeTagQuery(search.value);
      let visibleCount = 0;

      for (const tag of tags) {
        const tagName = tag.dataset.tagName || "";
        const label = tag.textContent || "";
        const isVisible = !query || tagName.includes(query) || normalizeTagQuery(label).includes(query);
        tag.hidden = !isVisible;
        if (isVisible) visibleCount += 1;
      }

      if (empty) empty.hidden = visibleCount !== 0;
    }

    search.addEventListener("input", applyFilter);
    applyFilter();
  }
}

function wireQualitySwitch() {
  const selector = document.querySelector("[data-quality-switch]");
  if (!selector) return;
  const video = document.querySelector("video.player");
  if (!video) return;

  let currentTime = 0;
  let wasPlaying = false;

  selector.addEventListener("change", () => {
    const quality = selector.value;
    const sources = Array.from(video.querySelectorAll("source[data-quality]"));

    currentTime = video.currentTime;
    wasPlaying = !video.paused;

    const target = sources.find((s) => s.dataset.quality === quality);
    if (!target) return;

    video.src = target.src;
    video.currentTime = currentTime;
    if (wasPlaying) {
      video.play().catch(() => {});
    }
  });
}

function wireVolumeSlider() {
  const slider = document.querySelector("[data-volume-slider]");
  const label = document.querySelector("[data-volume-label]");
  if (!slider || !label) return;
  if (document.querySelector("video.player")) return;

  function update() {
    const val = parseFloat(slider.value);
    const pct = Math.round(val * 100);
    label.textContent = `${pct}%`;
    slider.style.setProperty("--volume-pct", `${pct}%`);
  }

  slider.addEventListener("input", update);
  update();
}

function applyDefaultVolume() {
  const video = document.querySelector("video.player");
  if (!video) return;
  const el = document.querySelector("[data-default-volume]");
  if (!el) return;
  const vol = parseFloat(el.value);
  if (isNaN(vol)) return;

  function set() {
    video.volume = Math.max(0, Math.min(1, vol));
  }

  if (video.readyState >= 1) {
    set();
  } else {
    video.addEventListener("loadedmetadata", set, { once: true });
  }
}

function wireCustomVolume() {
  const video = document.querySelector("video.player");
  if (!video) return;

  const slider = document.querySelector("[data-volume-slider]");
  const label = document.querySelector("[data-volume-label]");
  const toggle = document.querySelector("[data-volume-toggle]");
  if (!slider) return;

  let prevVolume = 1;

  function sync() {
    const vol = video.volume;
    const pct = Math.round(vol * 100);
    slider.value = vol;
    slider.style.setProperty("--volume-pct", `${pct}%`);
    if (label) label.textContent = `${pct}%`;
    if (toggle) toggle.classList.toggle("muted", vol === 0);
  }

  slider.addEventListener("input", () => {
    video.volume = parseFloat(slider.value);
    sync();
  });

  if (toggle) {
    toggle.addEventListener("click", () => {
      if (video.volume > 0) {
        prevVolume = video.volume;
        video.volume = 0;
      } else {
        video.volume = prevVolume;
      }
      sync();
    });
  }

  video.addEventListener("volumechange", sync);

  function onReady() {
    sync();
  }

  if (video.readyState >= 1) {
    onReady();
  } else {
    video.addEventListener("loadedmetadata", onReady, { once: true });
  }

  document.addEventListener("keydown", (event) => {
    if (isTypingTarget(event.target)) return;
    if (event.altKey || event.metaKey || event.ctrlKey) return;
    if (document.querySelector("video.player") !== video) return;

    if (event.key === "ArrowUp") {
      event.preventDefault();
      video.volume = Math.min(1, video.volume + 0.1);
      sync();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      video.volume = Math.max(0, video.volume - 0.1);
      sync();
      return;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireSearchForm();
  wireUntaggedFilter();
  wireBulkActions();
  wireSimpleSelect();
  wireDropzone();
  wireThemeToggle();
  wireTagSearch();
  wireVideoPlayerShortcuts();
  wireVideoMeta();
  wireFolderToggles();
  wireQualitySwitch();
  wireVolumeSlider();
  applyDefaultVolume();
  wireCustomVolume();
});