// File picker with removable "x" bubbles.
//
// Original files (when editing a recalled request) are checkboxes named keep_files; the x unticks one
// (with an undo), so the server only takes over the ones still ticked. New uploads are held in a list:
// picking files again adds to it rather than replacing it, the x drops one, and the list is written back
// into the <input type=file> so a normal form submit sends exactly what's shown.
// Without JavaScript, the checkboxes and a plain multi-file input still work.
(function () {
  "use strict";
  var picker = document.querySelector("[data-file-picker]");
  if (!picker || typeof DataTransfer === "undefined") return;
  document.documentElement.classList.add("js");

  var input = picker.querySelector("input[type=file]");
  var chips = picker.querySelector("[data-chips]");
  var maxFiles = parseInt(picker.dataset.maxFiles, 10);
  var maxBytes = parseInt(picker.dataset.maxMb, 10) * 1024 * 1024;
  var maxTotalBytes = parseInt(picker.dataset.maxTotalMb, 10) * 1024 * 1024;
  var extensions = picker.dataset.extensions.toLowerCase().split(",");
  var selected = []; // File objects the user has added and not removed
  var errors = [];   // rejected picks, shown until dismissed

  function xButton(label, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "chip-x";
    b.setAttribute("aria-label", label);
    b.textContent = "×";
    b.addEventListener("click", onClick);
    return b;
  }

  // Original files: x toggles the keep checkbox.
  Array.prototype.forEach.call(chips.querySelectorAll("[data-original]"), function (chip) {
    var box = chip.querySelector("input[type=checkbox]");
    var name = chip.querySelector(".chip-name").textContent;
    var btn = xButton("Remove " + name, function () {
      box.checked = !box.checked;
      sync();
    });
    chip.appendChild(btn);
    function sync() {
      chip.classList.toggle("removed", !box.checked);
      btn.textContent = box.checked ? "×" : "undo";
      btn.classList.toggle("undo", !box.checked);
      btn.setAttribute("aria-label", (box.checked ? "Remove " : "Keep ") + name);
      updateCount();
    }
    box.addEventListener("change", sync);
    sync();
  });

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " bytes";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1048576).toFixed(1) + " MB";
  }

  function sameFile(a, b) {
    return a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;
  }

  function problemWith(file) {
    var dot = file.name.lastIndexOf(".");
    var ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (extensions.indexOf(ext) < 0) return "unsupported file type";
    if (file.size > maxBytes) return "larger than " + picker.dataset.maxMb + " MB";
    if (file.size === 0) return "empty file";
    return null;
  }

  input.addEventListener("change", function () {
    Array.prototype.forEach.call(input.files, function (file) {
      if (selected.some(function (f) { return sameFile(f, file); })) return;
      var problem = problemWith(file);
      if (problem) errors.push(file.name + ": " + problem);
      else selected.push(file);
    });
    render();
  });

  function keptCount() {
    return chips.querySelectorAll("[data-original] input:checked").length;
  }

  function uploadBytes() {
    return selected.reduce(function (sum, f) { return sum + f.size; }, 0);
  }

  // Why the current selection can't be submitted, or null. Kept originals aren't re-uploaded, so only new
  // files count towards the size limit (which a proxy like Cloudflare may enforce before we ever see it).
  function problem() {
    var total = keptCount() + selected.length;
    if (total > maxFiles) return "Too many files: the limit is " + maxFiles + ".";
    if (uploadBytes() > maxTotalBytes) {
      return "New files add up to " + formatSize(uploadBytes()) + "; the limit is " + picker.dataset.maxTotalMb +
        " MB per submission. Remove some, and submit them as a separate request.";
    }
    return null;
  }

  function updateCount() {
    var total = keptCount() + selected.length;
    var note = picker.querySelector("[data-count]");
    if (!note) {
      note = document.createElement("p");
      note.setAttribute("data-count", "");
      note.setAttribute("aria-live", "polite");
      chips.after(note);
    }
    var issue = problem();
    note.className = issue ? "errorlist" : "muted";
    note.textContent = issue || (total === 0 ? "No files selected yet."
      : total + (total === 1 ? " file" : " files") + (selected.length ? " (" + formatSize(uploadBytes()) + " to upload)" : ""));
  }

  picker.closest("form").addEventListener("submit", function (event) {
    if (problem()) {
      event.preventDefault();
      updateCount();
      picker.scrollIntoView({ block: "center" });
    }
  });

  function render() {
    // Write the list back into the real input so it's what gets submitted.
    var dt = new DataTransfer();
    selected.forEach(function (f) { dt.items.add(f); });
    input.files = dt.files;

    Array.prototype.forEach.call(chips.querySelectorAll("[data-new], [data-error]"), function (el) { el.remove(); });
    selected.forEach(function (file, i) {
      var li = document.createElement("li");
      li.className = "chip";
      li.setAttribute("data-new", "");
      var name = document.createElement("span");
      name.className = "chip-name";
      name.textContent = file.name;
      var size = document.createElement("span");
      size.className = "muted";
      size.textContent = formatSize(file.size);
      li.append(name, " ", size, xButton("Remove " + file.name, function () {
        selected.splice(i, 1);
        render();
      }));
      chips.appendChild(li);
    });
    errors.forEach(function (msg, i) {
      var li = document.createElement("li");
      li.className = "chip invalid";
      li.setAttribute("data-error", "");
      li.setAttribute("role", "alert");
      li.append(msg, xButton("Dismiss", function () {
        errors.splice(i, 1);
        render();
      }));
      chips.appendChild(li);
    });
    updateCount();
  }

  render();
})();
