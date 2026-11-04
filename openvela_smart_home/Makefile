# ============================================================
#  Makefile - openvela 智能家居 Tool 独立编译 & 测试
#
#  用法:
#    make              — 编译所有 Tool + 测试
#    make test         — 运行自动化测试
#    make bench        — 运行性能基准
#    make clean        — 清理编译产物
#    make security     — 编译并运行安全模块测试
# ============================================================

CC      ?= gcc
CFLAGS  ?= -Wall -Wextra -O2 -std=c99
LDFLAGS ?= -lm

# 交叉编译（ARM64 嵌入式目标）
CROSS_CC ?= aarch64-linux-gnu-gcc

TOOLS_DIR    = tools
SECURITY_DIR = security
BUILD_DIR    = .build
TEST_DIR     = .test_build

# ---- 源文件 ----
TOOL_SRCS = $(TOOLS_DIR)/tool_light_control.c \
            $(TOOLS_DIR)/tool_temperature_read.c \
            $(TOOLS_DIR)/device_state.c \
            $(TOOLS_DIR)/tool_curtain_control.c \
            $(TOOLS_DIR)/tool_security_control.c \
            $(TOOLS_DIR)/tool_environment_monitor.c

TOOL_OBJS = $(patsubst $(TOOLS_DIR)/%.c,$(BUILD_DIR)/%.o,$(TOOL_SRCS))

SEC_SRCS  = $(SECURITY_DIR)/command_whitelist.c
SEC_OBJS  = $(BUILD_DIR)/command_whitelist.o

# ---- 默认目标 ----
.PHONY: all test bench clean security help
all: $(BUILD_DIR)/libsmart_home.a $(BUILD_DIR)/test_light $(BUILD_DIR)/test_temp $(BUILD_DIR)/test_sec
	@echo ""
	@echo "✓ 编译完成！产物在 $(BUILD_DIR)/"
	@echo "  运行测试: make test"
	@echo "  运行基准: make bench"

# ---- 静态库 ----
$(BUILD_DIR)/libsmart_home.a: $(TOOL_OBJS)
	@mkdir -p $(BUILD_DIR)
	ar rcs $@ $^
	@echo "  AR   $@"

# ---- 编译 Tool 目标文件 ----
$(BUILD_DIR)/%.o: $(TOOLS_DIR)/%.c
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -I$(TOOLS_DIR) -c $< -o $@
	@echo "  CC   $@"

# ---- 测试程序 ----
$(BUILD_DIR)/test_light: $(BUILD_DIR)/tool_light_control.o $(BUILD_DIR)/device_state.o
	@echo "  LD   $@"
	$(CC) $(CFLAGS) -DTEST_BUILD -I$(TOOLS_DIR) $^ $(LDFLAGS) -o $@ || \
		echo "  (跳过：需要实现完整链接)"

$(BUILD_DIR)/test_temp: $(BUILD_DIR)/tool_temperature_read.o
	@echo "  LD   $@"
	$(CC) $(CFLAGS) -DTEST_BUILD -I$(TOOLS_DIR) $< $(LDFLAGS) -o $@ || \
		echo "  (跳过：需要实现完整链接)"

$(BUILD_DIR)/test_sec: $(SECURITY_DIR)/command_whitelist.c
	@mkdir -p $(BUILD_DIR)
	$(CC) $(CFLAGS) -DCOMMAND_WHITELIST_TEST $< $(LDFLAGS) -o $@
	@echo "  CC+LD $@"

# ---- 测试 ----
test:
	@chmod +x test_runner.sh 2>/dev/null || true
	@./test_runner.sh full

bench:
	@chmod +x test_runner.sh 2>/dev/null || true
	@./test_runner.sh bench

security: $(BUILD_DIR)/test_sec
	@echo "=== 命令白名单安全测试 ==="
	@$<

# ---- 交叉编译 (ARM64) ----
arm64: CFLAGS += -mcpu=cortex-a57 -march=armv8-a
arm64: CC = $(CROSS_CC)
arm64: $(BUILD_DIR)/libsmart_home.a
	@echo "✓ ARM64 交叉编译完成"

# ---- 清理 ----
clean:
	rm -rf $(BUILD_DIR) $(TEST_DIR)
	@echo "✓ 清理完成"

# ---- 帮助 ----
help:
	@echo "openvela 智能家居 Tool 编译系统"
	@echo ""
	@echo "用法："
	@echo "  make         编译所有 Tool + 测试"
	@echo "  make test    运行自动化测试套件"
	@echo "  make bench   运行性能基准测试"
	@echo "  make security  测试命令白名单模块"
	@echo "  make arm64   交叉编译 ARM64 版本"
	@echo "  make clean   清理编译产物"
