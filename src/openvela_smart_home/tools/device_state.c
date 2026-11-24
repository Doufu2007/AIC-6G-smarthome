/*
 * device_state.c - 设备状态持久化模块（状态记忆）
 *
 * 功能：
 *   1. 每次 Tool 操作后自动保存设备状态到 device_state.json
 *   2. 模块初始化时自动从 device_state.json 恢复上次状态
 *   3. 支持导出当前状态为 JSON（供 Web Dashboard API 使用）
 *
 * 设计目标：
 *   - 赋予 Agent "记忆能力"，支持上下文感知操作
 *   - 用户说"调暗一点"时，Agent 能自动继承上次亮度上下文
 *   - Web Dashboard 可实时读取设备状态
 *
 * v2.2 修复：
 *   - ensure_state_initialized: 初始化窗帘/安防/传感器字段
 *   - device_state_save: 保存窗帘和安防状态
 *   - device_state_load: 恢复窗帘和安防状态
 *   - device_state_to_json: 防缓冲区溢出（snprintf 边界检查）
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "smart_home_tools.h"

/* 全局设备状态（在此文件维护，tool_light_control.c 引用） */
static device_state_t g_device_state;

/* ---------- 内部辅助 ---------- */

/*
 * 确保设备状态已初始化（延迟初始化）
 *
 * 修复：初始化所有字段，包括窗帘/安防/传感器/安全模式，
 * 避免使用未初始化的内存。
 */
static void ensure_state_initialized(void) {
    if (g_device_state.initialized) return;

    for (int i = 0; i < 6; i++) {
        /* 灯光 */
        g_device_state.lights[i].is_on      = false;
        g_device_state.lights[i].brightness  = 0;
        /* 温湿度 */
        g_device_state.last_temp[i]          = 25.0f;
        g_device_state.last_hum[i]           = 60.0f;
        /* 窗帘 */
        g_device_state.curtains[i].is_open   = (i != LOCATION_BATHROOM);
        g_device_state.curtains[i].position  = g_device_state.curtains[i].is_open ? 100 : 0;
        /* 安防 */
        g_device_state.security[i].locked        = true;
        g_device_state.security[i].window_closed = (i != LOCATION_BATHROOM);
    }
    /* 传感器数据：初始化为零（首次读取时才填充真实数据） */
    memset(g_device_state.sensors, 0, sizeof(g_device_state.sensors));
    g_device_state.security_mode = SECURITY_MODE_OFF;
    g_device_state.initialized = 1;
}

/*
 * 获取位置的中文名称
 */
static const char* loc_name(int idx) {
    const char *names[] = {"客厅", "卧室", "厨房", "浴室", "书房", "室外"};
    return (idx >= 0 && idx < 6) ? names[idx] : "未知";
}

/* ---------- 持久化接口 ---------- */

/*
 * device_state_save - 将当前设备状态写入 JSON 文件
 *
 * 修复：新增窗帘(curtains)和安防(security)状态的持久化。
 */
int device_state_save(const char *filepath) {
    ensure_state_initialized();

    FILE *fp = fopen(filepath, "w");
    if (!fp) {
        fprintf(stderr, "[StateSave] 错误: 无法写入 %s\n", filepath);
        return TOOL_ERR_IO;
    }

    fprintf(fp, "{\n");

    /* lights 部分 */
    fprintf(fp, "  \"lights\": {\n");
    for (int i = 0; i < 6; i++) {
        fprintf(fp, "    \"%s\": {\"is_on\": %s, \"brightness\": %d}%s\n",
                loc_name(i),
                g_device_state.lights[i].is_on ? "true" : "false",
                g_device_state.lights[i].brightness,
                (i < 5) ? "," : "");
    }
    fprintf(fp, "  },\n");

    /* temperature 部分 */
    fprintf(fp, "  \"temperature\": {\n");
    for (int i = 0; i < 6; i++) {
        fprintf(fp, "    \"%s\": {\"temp\": %.1f, \"humidity\": %.1f}%s\n",
                loc_name(i),
                g_device_state.last_temp[i],
                g_device_state.last_hum[i],
                (i < 5) ? "," : "");
    }
    fprintf(fp, "  },\n");

    /* curtains 部分 */
    fprintf(fp, "  \"curtains\": {\n");
    for (int i = 0; i < 6; i++) {
        fprintf(fp, "    \"%s\": {\"is_open\": %s, \"position\": %d}%s\n",
                loc_name(i),
                g_device_state.curtains[i].is_open ? "true" : "false",
                g_device_state.curtains[i].position,
                (i < 5) ? "," : "");
    }
    fprintf(fp, "  },\n");

    /* security 部分 */
    fprintf(fp, "  \"security\": {\n");
    fprintf(fp, "    \"mode\": %d,\n", (int)g_device_state.security_mode);
    fprintf(fp, "    \"zones\": {\n");
    for (int i = 0; i < 6; i++) {
        fprintf(fp, "      \"%s\": {\"locked\": %s, \"window_closed\": %s}%s\n",
                loc_name(i),
                g_device_state.security[i].locked ? "true" : "false",
                g_device_state.security[i].window_closed ? "true" : "false",
                (i < 5) ? "," : "");
    }
    fprintf(fp, "    }\n");
    fprintf(fp, "  }\n");

    fprintf(fp, "}\n");

    fclose(fp);
    fprintf(stdout, "[StateSave] 设备状态已保存至 %s\n", filepath);
    return TOOL_SUCCESS;
}

/*
 * device_state_load - 从 JSON 文件恢复设备状态
 *
 * 修复：新增窗帘(curtains)和安防(security)状态的恢复。
 */
int device_state_load(const char *filepath) {
    ensure_state_initialized();

    FILE *fp = fopen(filepath, "r");
    if (!fp) {
        fprintf(stdout, "[StateLoad] %s 不存在，使用默认状态\n", filepath);
        return TOOL_ERR_IO;  /* 文件不存在不是致命错误 */
    }

    /* 简易 JSON 解析（不依赖第三方库） */
    char line[256];
    char current_loc[32] = "";
    int in_lights = 0, in_temp = 0, in_curtains = 0, in_sec_zones = 0;

    while (fgets(line, sizeof(line), fp)) {
        /* 检测节 */
        if (strstr(line, "\"lights\""))       { in_lights = 1; in_temp = 0; in_curtains = 0; in_sec_zones = 0; continue; }
        if (strstr(line, "\"temperature\""))   { in_lights = 0; in_temp = 1; in_curtains = 0; in_sec_zones = 0; continue; }
        if (strstr(line, "\"curtains\""))      { in_lights = 0; in_temp = 0; in_curtains = 1; in_sec_zones = 0; continue; }
        if (strstr(line, "\"zones\""))         { in_lights = 0; in_temp = 0; in_curtains = 0; in_sec_zones = 1; continue; }

        /* security mode (非 zones 节) */
        if (!in_sec_zones && strstr(line, "\"mode\"")) {
            char *colon = strchr(line, ':');
            if (colon) g_device_state.security_mode = (security_mode_t)atoi(colon + 1);
            continue;
        }

        /* 提取位置名 */
        char *q1 = strchr(line, '"');
        char *q2 = q1 ? strchr(q1 + 1, '"') : NULL;
        if (q1 && q2) {
            size_t len = q2 - q1 - 1;
            if (len > 0 && len < sizeof(current_loc)) {
                memcpy(current_loc, q1 + 1, len);
                current_loc[len] = '\0';
            }
        }

        /* 找到位置对应的索引 */
        int idx = -1;
        for (int i = 0; i < 6; i++) {
            if (strcmp(current_loc, loc_name(i)) == 0) { idx = i; break; }
        }
        if (idx < 0) continue;

        /* 解析值 */
        if (in_lights) {
            char *on_ptr  = strstr(line, "\"is_on\"");
            char *bri_ptr = strstr(line, "\"brightness\"");

            if (on_ptr) {
                g_device_state.lights[idx].is_on = (strstr(on_ptr, "true") != NULL);
            }
            if (bri_ptr) {
                char *colon = strchr(bri_ptr, ':');
                if (colon) {
                    g_device_state.lights[idx].brightness = (uint8_t)atoi(colon + 1);
                }
            }
        }
        if (in_temp) {
            char *temp_ptr = strstr(line, "\"temp\"");
            char *hum_ptr  = strstr(line, "\"humidity\"");

            if (temp_ptr) {
                char *colon = strchr(temp_ptr, ':');
                if (colon) g_device_state.last_temp[idx] = (float)atof(colon + 1);
            }
            if (hum_ptr) {
                char *colon = strchr(hum_ptr, ':');
                if (colon) g_device_state.last_hum[idx] = (float)atof(colon + 1);
            }
        }
        if (in_curtains) {
            char *open_ptr = strstr(line, "\"is_open\"");
            char *pos_ptr  = strstr(line, "\"position\"");

            if (open_ptr) {
                g_device_state.curtains[idx].is_open = (strstr(open_ptr, "true") != NULL);
            }
            if (pos_ptr) {
                char *colon = strchr(pos_ptr, ':');
                if (colon) {
                    g_device_state.curtains[idx].position = (uint8_t)atoi(colon + 1);
                }
            }
        }
        if (in_sec_zones) {
            char *lock_ptr = strstr(line, "\"locked\"");
            char *win_ptr  = strstr(line, "\"window_closed\"");

            if (lock_ptr) {
                g_device_state.security[idx].locked = (strstr(lock_ptr, "true") != NULL);
            }
            if (win_ptr) {
                g_device_state.security[idx].window_closed = (strstr(win_ptr, "true") != NULL);
            }
        }
    }

    fclose(fp);
    fprintf(stdout, "[StateLoad] 设备状态已从 %s 恢复\n", filepath);
    return TOOL_SUCCESS;
}

/*
 * device_state_to_json - 导出当前状态为完整 JSON 字符串
 *   供 Web Dashboard API 使用
 *
 * 修复：每次 snprintf 后检查 offset 边界，
 * 防止缓冲区溢出和 size_t 回绕导致的段错误。
 */
int device_state_to_json(char *buffer, size_t buf_size) {
    ensure_state_initialized();

    if (buf_size == 0) return TOOL_ERR_PARAM;
    buffer[0] = '\0';

    int offset = 0; int ret;
    #define SAFE_APPEND(fmt, ...) do { \
        if ((size_t)offset >= buf_size) break; \
        ret = snprintf(buffer + offset, buf_size - offset, fmt, ##__VA_ARGS__); \
        if (ret < 0) break; \
        offset += ret; \
    } while(0)

    SAFE_APPEND("{\n");
    SAFE_APPEND("  \"lights\": {\n");
    for (int i = 0; i < 6; i++) {
        SAFE_APPEND("    \"%s\": {\"is_on\": %s, \"brightness\": %d}%s\n",
                loc_name(i),
                g_device_state.lights[i].is_on ? "true" : "false",
                g_device_state.lights[i].brightness,
                (i < 5) ? "," : "");
    }
    SAFE_APPEND("  },\n");
    SAFE_APPEND("  \"temperature\": {\n");
    for (int i = 0; i < 6; i++) {
        SAFE_APPEND("    \"%s\": {\"temp\": %.1f, \"humidity\": %.1f}%s\n",
                loc_name(i),
                g_device_state.last_temp[i],
                g_device_state.last_hum[i],
                (i < 5) ? "," : "");
    }
    SAFE_APPEND("  }\n");
    SAFE_APPEND("}\n");

    #undef SAFE_APPEND

    /* 确保结尾有 null */
    if ((size_t)offset >= buf_size)
        buffer[buf_size - 1] = '\0';
    else
        buffer[offset] = '\0';

    return TOOL_SUCCESS;
}

/*
 * device_state_update_light - 更新单灯光状态（供 tool_light_control.c 调用）
 */
int device_state_update_light(int loc_idx, bool is_on, uint8_t brightness) {
    ensure_state_initialized();
    if (loc_idx < 0 || loc_idx >= 6) return TOOL_ERR_PARAM;

    g_device_state.lights[loc_idx].is_on      = is_on;
    g_device_state.lights[loc_idx].brightness  = brightness;
    return TOOL_SUCCESS;
}

/*
 * device_state_update_temp - 更新温湿度记录（供 tool_temperature_read.c 调用）
 */
int device_state_update_temp(int loc_idx, float temp, float humidity) {
    ensure_state_initialized();
    if (loc_idx < 0 || loc_idx >= 6) return TOOL_ERR_PARAM;

    g_device_state.last_temp[loc_idx] = temp;
    g_device_state.last_hum[loc_idx]  = humidity;
    return TOOL_SUCCESS;
}

/*
 * device_state_get_light - 读取灯光状态（供外部查询）
 */
int device_state_get_light(int loc_idx, bool *is_on, uint8_t *brightness) {
    ensure_state_initialized();
    if (loc_idx < 0 || loc_idx >= 6) return TOOL_ERR_PARAM;

    if (is_on)      *is_on      = g_device_state.lights[loc_idx].is_on;
    if (brightness) *brightness  = g_device_state.lights[loc_idx].brightness;
    return TOOL_SUCCESS;
}

/*
 * device_state_get_temp - 读取最近温湿度（供上下文感知使用）
 */
int device_state_get_temp(int loc_idx, float *temp, float *humidity) {
    ensure_state_initialized();
    if (loc_idx < 0 || loc_idx >= 6) return TOOL_ERR_PARAM;

    if (temp)     *temp     = g_device_state.last_temp[loc_idx];
    if (humidity) *humidity = g_device_state.last_hum[loc_idx];
    return TOOL_SUCCESS;
}
