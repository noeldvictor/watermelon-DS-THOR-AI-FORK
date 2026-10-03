/*
    Copyright 2016-2026 melonDS team

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.

    melonDS is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with melonDS. If not, see http://www.gnu.org/licenses/.
*/

#include "HDPackSource.h"

#include "Platform.h"
#include "astcenc.h"
#include "miniz.h"

#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <unordered_map>

namespace melonDS
{

namespace fs = std::filesystem;

namespace
{

class DirSource final : public HDPackSource
{
public:
    explicit DirSource(std::string root) : Root(std::move(root)) {}

    std::vector<std::string> List(const std::string& prefix) const override
    {
        std::vector<std::string> out;
        std::error_code ec;
        // the prefix is a folder ("textures/"): walk that one, not the whole pack
        const std::string sub = prefix.substr(0, prefix.rfind('/') == std::string::npos ? 0 : prefix.rfind('/'));
        const fs::path start = fs::u8path(sub.empty() ? Root : Root + "/" + sub);
        if (!fs::is_directory(start, ec))
            return out;
        const fs::path root = fs::u8path(Root);
        for (auto it = fs::recursive_directory_iterator(start, ec); it != fs::recursive_directory_iterator(); it.increment(ec))
        {
            if (ec) break;
            if (!it->is_regular_file(ec)) continue;
            std::string rel = it->path().lexically_relative(root).generic_u8string();
            if (rel.compare(0, prefix.size(), prefix) == 0)
                out.push_back(std::move(rel));
        }
        return out;
    }

    bool Read(const std::string& name, std::vector<u8>& out) const override
    {
        std::ifstream in(fs::u8path(Root + "/" + name), std::ios::binary);
        if (!in)
            return false;
        out.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
        return true;
    }

    bool ReadHead(const std::string& name, size_t count, std::vector<u8>& out) const override
    {
        std::ifstream in(fs::u8path(Root + "/" + name), std::ios::binary);
        if (!in)
            return false;
        out.resize(count);
        in.read(reinterpret_cast<char*>(out.data()), (std::streamsize)count);
        out.resize((size_t)in.gcount());
        return true;
    }

    bool IsZip() const override { return false; }
    const std::string& Location() const override { return Root; }

private:
    std::string Root;
};

class ZipSource final : public HDPackSource
{
public:
    explicit ZipSource(std::string path) : Path(std::move(path))
    {
        memset(&Zip, 0, sizeof(Zip));
        Ok = mz_zip_reader_init_file(&Zip, Path.c_str(), 0);
        if (!Ok)
        {
            Platform::Log(Platform::LogLevel::Warn, "HDPackSource: could not open %s (%s)\n",
                          Path.c_str(), mz_zip_get_error_string(mz_zip_get_last_error(&Zip)));
            return;
        }
        const mz_uint count = mz_zip_reader_get_num_files(&Zip);
        Names.reserve(count);
        for (mz_uint i = 0; i < count; i++)
        {
            if (mz_zip_reader_is_file_a_directory(&Zip, i))
                continue;
            char name[512];
            if (!mz_zip_reader_get_filename(&Zip, i, name, sizeof(name)))
                continue;
            Index[name] = i;
            Names.emplace_back(name);
        }
    }

    ~ZipSource() override
    {
        if (Ok)
            mz_zip_reader_end(&Zip);
    }

    std::vector<std::string> List(const std::string& prefix) const override
    {
        std::vector<std::string> out;
        for (const std::string& n : Names)
            if (n.compare(0, prefix.size(), prefix) == 0)
                out.push_back(n);
        return out;
    }

    bool Read(const std::string& name, std::vector<u8>& out) const override
    {
        auto it = Index.find(name);
        if (it == Index.end())
            return false;
        // a miniz reader is one file handle: one extraction at a time
        std::lock_guard<std::mutex> lock(Lock);
        size_t size = 0;
        void* data = mz_zip_reader_extract_to_heap(&Zip, it->second, &size, 0);
        if (!data)
            return false;
        out.assign(static_cast<u8*>(data), static_cast<u8*>(data) + size);
        mz_free(data);
        return true;
    }

    bool ReadHead(const std::string& name, size_t count, std::vector<u8>& out) const override
    {
        auto it = Index.find(name);
        if (it == Index.end())
            return false;
        std::lock_guard<std::mutex> lock(Lock);
        mz_zip_reader_extract_iter_state* iter = mz_zip_reader_extract_iter_new(&Zip, it->second, 0);
        if (!iter)
            return false;
        out.resize(count);
        out.resize(mz_zip_reader_extract_iter_read(iter, out.data(), count));
        mz_zip_reader_extract_iter_free(iter);
        return true;
    }

    bool IsZip() const override { return true; }
    const std::string& Location() const override { return Path; }

private:
    std::string Path;
    mutable mz_zip_archive Zip;
    mutable std::mutex Lock;
    bool Ok = false;
    std::unordered_map<std::string, mz_uint> Index;
    std::vector<std::string> Names;
};

}

std::unique_ptr<HDPackSource> HDPackSource::Open(const std::string& packDir)
{
    std::error_code ec;
    const std::string zip = packDir + ".zip";
    if (fs::is_regular_file(fs::u8path(zip), ec))
        return std::make_unique<ZipSource>(zip);
    return std::make_unique<DirSource>(packDir);
}

bool AstcImageSize(const std::vector<u8>& head, u32& width, u32& height)
{
    if (head.size() < 16 || head[0] != 0x13 || head[1] != 0xAB || head[2] != 0xA1 || head[3] != 0x5C)
        return false;
    width = head[7] | (head[8] << 8) | (head[9] << 16);
    height = head[10] | (head[11] << 8) | (head[12] << 16);
    return width && height && head[4] && head[5] && head[6] == 1;
}

bool DecodeAstc(const std::vector<u8>& file, std::vector<u32>& rgba, u32& width, u32& height)
{
    if (!AstcImageSize(file, width, height))
        return false;
    const u32 bx = file[4], by = file[5];
    const size_t blocks = (size_t)((width + bx - 1) / bx) * ((height + by - 1) / by);
    if (file.size() < 16 + blocks * 16)
        return false;

    // one decompress-only context per block size, kept: building one sets up all its tables
    static std::mutex lock;
    static std::map<u32, astcenc_context*> contexts;
    std::lock_guard<std::mutex> guard(lock);
    astcenc_context*& ctx = contexts[(bx << 8) | by];
    if (!ctx)
    {
        astcenc_config config;
        if (astcenc_config_init(ASTCENC_PRF_LDR, bx, by, 1, ASTCENC_PRE_FASTEST,
                                ASTCENC_FLG_DECOMPRESS_ONLY, &config) != ASTCENC_SUCCESS
            || astcenc_context_alloc(&config, 1, &ctx, nullptr) != ASTCENC_SUCCESS)
        {
            ctx = nullptr;
            return false;
        }
    }

    rgba.resize((size_t)width * height);
    void* slice = rgba.data();
    astcenc_image image{width, height, 1, ASTCENC_TYPE_U8, &slice};
    const astcenc_swizzle swizzle{ASTCENC_SWZ_R, ASTCENC_SWZ_G, ASTCENC_SWZ_B, ASTCENC_SWZ_A};
    const astcenc_error err = astcenc_decompress_image(ctx, file.data() + 16, blocks * 16, &image, &swizzle, 0);
    astcenc_decompress_reset(ctx);
    return err == ASTCENC_SUCCESS;
}

bool PngImageSize(const std::vector<u8>& head, u32& width, u32& height)
{
    static const u8 sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
    if (head.size() < 24 || memcmp(head.data(), sig, 8) || memcmp(&head[12], "IHDR", 4))
        return false;
    width = (head[16] << 24) | (head[17] << 16) | (head[18] << 8) | head[19];
    height = (head[20] << 24) | (head[21] << 16) | (head[22] << 8) | head[23];
    return width && height;
}

}
