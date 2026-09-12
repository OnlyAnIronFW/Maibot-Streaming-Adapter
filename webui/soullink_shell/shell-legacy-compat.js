// Compatibility layer for the trimmed SoulLink frontend bundle.
// We keep SoulLink's runtime logic intact and only provide the globals that
// the original loader expects from omitted UI/occlusion modules.

function ensureModelContainer() {
  if (typeof PIXI === "undefined" || !app) {
    return null;
  }

  if (!modelContainer) {
    modelContainer = new PIXI.Container();
  }

  if (!app.stage.children.includes(modelContainer)) {
    app.stage.addChild(modelContainer);
  }

  window.modelContainer = modelContainer;
  return modelContainer;
}

window.initOcclusionLayers =
  window.initOcclusionLayers ||
  function initOcclusionLayersCompat() {
    ensureModelContainer();
    foregroundSprite = foregroundSprite || null;
    occlusionMask = occlusionMask || null;
    maskEditorLayer = maskEditorLayer || null;
    maskOutline = maskOutline || null;
    maskDragArea = maskDragArea || null;
    maskHandleNodes = maskHandleNodes || [];
    occlusionMode = occlusionMode || "none";
    occlusionState =
      occlusionState || {
        topEdgePoints: [],
        offsetY: 0,
        showHandles: false,
        showMaskLine: false,
        enableMaskDrag: false,
        addNodeMode: false,
        extractedMaskTexture: null,
        aiMaskSprite: null,
        showAIOutline: false,
      };
    window.foregroundSprite = foregroundSprite;
    window.occlusionMask = occlusionMask;
    window.maskEditorLayer = maskEditorLayer;
    window.maskOutline = maskOutline;
    window.maskDragArea = maskDragArea;
    window.maskHandleNodes = maskHandleNodes;
    window.occlusionMode = occlusionMode;
    window.occlusionState = occlusionState;
  };

window.generateControlPanel =
  window.generateControlPanel ||
  function generateControlPanelCompat() {
    window.dispatchEvent(new CustomEvent("soullink-shell:legacy-control-panel-ready"));
  };

window.updateForegroundSprite =
  window.updateForegroundSprite ||
  function updateForegroundSpriteCompat() {};

window.syncForegroundToBackground =
  window.syncForegroundToBackground ||
  function syncForegroundToBackgroundCompat() {};

window.redrawOcclusionMask =
  window.redrawOcclusionMask ||
  function redrawOcclusionMaskCompat() {};

window.disableOcclusion =
  window.disableOcclusion ||
  function disableOcclusionCompat() {
    occlusionMode = "none";
    window.occlusionMode = occlusionMode;
  };
