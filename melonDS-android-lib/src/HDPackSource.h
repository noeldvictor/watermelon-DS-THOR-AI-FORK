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

#ifndef HDPACKSOURCE_H
#define HDPACKSOURCE_H

#include "types.h"

#include <memory>
#include <string>
#include <vector>

namespace melonDS
{

// The files of an HD pack. The standard pack is one zip per game, texturepacks/<CODE>.zip,
// holding ASTC 4x4 images (.astc) under textures/, sprites/, bgtiles/, fonts/ and models/,
// the pack's text files, and pack.txt ("scale <N>"). A folder texturepacks/<CODE>/ with the
// same layout (PNG or ASTC) is read the same way: work in progress, and the filter disk cache.
class HDPackSource
{
public:
    virtual ~HDPackSource() = default;

    // relative names ('/'-separated) of every file whose name starts with prefix ("textures/")
    virtual std::vector<std::string> List(const std::string& prefix) const = 0;
    virtual bool Read(const std::string& name, std::vector<u8>& out) const = 0;
    // the first `count` bytes of a file (image headers), fewer if the file is shorter
    virtual bool ReadHead(const std::string& name, size_t count, std::vector<u8>& out) const = 0;
    virtual bool IsZip() const = 0;
    // where it reads from, for logs
    virtual const std::string& Location() const = 0;

    // <packDir>.zip when it exists, else the folder <packDir> (which may not exist: then the
    // source is just empty)
    static std::unique_ptr<HDPackSource> Open(const std::string& packDir);
};

// ASTC files as astcenc writes them: a 16-byte header (magic 13 AB A1 5C, block size x/y/z,
// then 24-bit width, height, depth), then 16-byte blocks in rows.
bool AstcImageSize(const std::vector<u8>& head, u32& width, u32& height);
// Decodes a 2D LDR ASTC file (any 2D block size) to r | g<<8 | b<<16 | a<<24 pixels.
bool DecodeAstc(const std::vector<u8>& file, std::vector<u32>& rgba, u32& width, u32& height);
// Width and height from a PNG's first 24 bytes (signature + IHDR).
bool PngImageSize(const std::vector<u8>& head, u32& width, u32& height);

}

#endif
