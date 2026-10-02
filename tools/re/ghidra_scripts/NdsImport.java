// Sets up a Nintendo DS program before analysis (run as -preScript by tools/re/re.py): memory blocks
// from a plan file, I/O register labels, entry points (ARM or Thumb) and analysis options.
//
// Plan (JSON): {"blocks": [{"name", "address", "file", "primary", "overlay"}],
//   "uninitialized": [{"name", "address", "size", "volatile"}], "labels": [{"address", "name", "comment"}],
//   "entries": [{"address", "name", "thumb"}], "options": {"analyzer option": "value"}}
// Addresses are hex strings. Files are relative to the plan. The primary block is the file the
// importer already loaded; it only gets its name and permissions.
//@category NDS
import java.io.ByteArrayInputStream;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.mem.MemoryConflictException;
import ghidra.program.model.symbol.SourceType;

public class NdsImport extends GhidraScript {

    private Address address(String text) {
        return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(Long.decode(text));
    }

    @Override
    protected void run() throws Exception {
        Path planPath = Path.of(getScriptArgs()[0]);
        JsonObject plan = JsonParser.parseString(Files.readString(planPath)).getAsJsonObject();
        Path base = planPath.getParent();
        Memory memory = currentProgram.getMemory();

        if (plan.has("blocks")) {
            for (JsonElement element : plan.getAsJsonArray("blocks")) {
                JsonObject b = element.getAsJsonObject();
                String name = b.get("name").getAsString();
                Address address = address(b.get("address").getAsString());
                MemoryBlock block;
                if (b.has("primary") && b.get("primary").getAsBoolean()) {
                    block = memory.getBlock(address);
                    if (block == null) {
                        printerr("primary block not found at " + address);
                        continue;
                    }
                    block.setName(name);
                }
                else {
                    byte[] bytes = Files.readAllBytes(base.resolve(b.get("file").getAsString()));
                    boolean overlay = b.has("overlay") && b.get("overlay").getAsBoolean();
                    try {
                        block = memory.createInitializedBlock(name, address, new ByteArrayInputStream(bytes),
                            bytes.length, monitor, overlay);
                    }
                    catch (MemoryConflictException e) {
                        // code that shares addresses with something already mapped (overlays, autoloads
                        // into main RAM) gets its own address space
                        block = memory.createInitializedBlock(name, address, new ByteArrayInputStream(bytes),
                            bytes.length, monitor, true);
                    }
                }
                block.setRead(true);
                block.setWrite(true);
                block.setExecute(true);
                println("block " + name + " at " + block.getStart() + " (" + block.getSize() + " bytes)");
            }
        }

        if (plan.has("uninitialized")) {
            for (JsonElement element : plan.getAsJsonArray("uninitialized")) {
                JsonObject b = element.getAsJsonObject();
                Address address = address(b.get("address").getAsString());
                long size = Long.decode(b.get("size").getAsString());
                try {
                    MemoryBlock block = memory.createUninitializedBlock(b.get("name").getAsString(), address, size, false);
                    block.setRead(true);
                    block.setWrite(true);
                    block.setVolatile(b.has("volatile") && b.get("volatile").getAsBoolean());
                }
                catch (MemoryConflictException e) {
                    printerr("skipped block " + b.get("name").getAsString() + ": " + e.getMessage());
                }
            }
        }

        if (plan.has("labels")) {
            for (JsonElement element : plan.getAsJsonArray("labels")) {
                JsonObject l = element.getAsJsonObject();
                Address address = address(l.get("address").getAsString());
                if (!memory.contains(address)) {
                    continue;
                }
                if (l.has("name")) {
                    createLabel(address, l.get("name").getAsString(), true, SourceType.IMPORTED);
                }
                if (l.has("comment")) {
                    setPlateComment(address, l.get("comment").getAsString());
                }
            }
        }

        if (plan.has("entries")) {
            Register tmode = currentProgram.getRegister("TMode");
            for (JsonElement element : plan.getAsJsonArray("entries")) {
                JsonObject e = element.getAsJsonObject();
                Address address = address(e.get("address").getAsString());
                if (!memory.contains(address)) {
                    continue;
                }
                boolean thumb = e.has("thumb") && e.get("thumb").getAsBoolean();
                if (tmode != null) {
                    currentProgram.getProgramContext().setValue(tmode, address, address,
                        thumb ? BigInteger.ONE : BigInteger.ZERO);
                }
                currentProgram.getSymbolTable().addExternalEntryPoint(address);
                String name = e.has("name") ? e.get("name").getAsString() : null;
                if (name != null) {
                    createLabel(address, name, true, SourceType.IMPORTED);
                }
                disassemble(address);
                if (getFunctionAt(address) == null) {
                    createFunction(address, name);
                }
            }
        }

        if (plan.has("options")) {
            for (Map.Entry<String, JsonElement> option : plan.getAsJsonObject("options").entrySet()) {
                try {
                    setAnalysisOption(currentProgram, option.getKey(), option.getValue().getAsString());
                }
                catch (Exception e) {
                    printerr("analysis option " + option.getKey() + ": " + e.getMessage());
                }
            }
        }
    }
}
