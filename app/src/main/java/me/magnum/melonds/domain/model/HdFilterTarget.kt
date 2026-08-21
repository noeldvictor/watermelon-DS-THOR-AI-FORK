package me.magnum.melonds.domain.model

/**
 * The three producers that can be upscaled independently. Each maps to the
 * preference key the renderer reads its filter mode from, so the in-game
 * overlay and the settings screen stay in sync without a second source of
 * truth.
 */
enum class HdFilterTarget(val preferenceKey: String) {
    TEXTURE_3D("video_hd_texture_filter"),
    OBJ_SPRITE("video_obj_sprite_filter"),
    BG_LAYER("video_bg_layer_filter"),
}
