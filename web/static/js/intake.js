/**
 * COMP 1 — Matter Intake Drag-and-Drop Handler.
 *
 * Enhances the drop zone with visual feedback, file validation,
 * and multi-document support.
 */
(function () {
  'use strict';

  const ALLOWED_TYPES = [
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain',
  ];
  const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

  document.addEventListener('DOMContentLoaded', function () {
    const dropZone = document.getElementById('drop-zone');
    if (!dropZone) return;

    // Prevent browser default file open behavior
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
      document.body.addEventListener(eventName, function (e) {
        e.preventDefault();
        e.stopPropagation();
      });
    });

    // Visual feedback
    ['dragenter', 'dragover'].forEach(eventName => {
      dropZone.addEventListener(eventName, function () {
        dropZone.classList.add('border-blue-500', 'bg-blue-50');
      });
    });

    ['dragleave', 'drop'].forEach(eventName => {
      dropZone.addEventListener(eventName, function () {
        dropZone.classList.remove('border-blue-500', 'bg-blue-50');
      });
    });
  });

  /**
   * Validate files before processing.
   */
  window.validateFiles = function (files) {
    const errors = [];
    const valid = [];

    Array.from(files).forEach(function (f) {
      if (f.size > MAX_FILE_SIZE) {
        errors.push(f.name + ' exceeds 50MB limit');
      } else if (ALLOWED_TYPES.length > 0 && !ALLOWED_TYPES.includes(f.type) && !f.name.match(/\.(pdf|doc|docx|txt)$/i)) {
        errors.push(f.name + ' is not a supported format');
      } else {
        valid.push(f);
      }
    });

    if (errors.length > 0) {
      alert('File errors:\n' + errors.join('\n'));
    }

    return valid;
  };
})();
