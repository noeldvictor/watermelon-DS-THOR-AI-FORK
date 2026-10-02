// Queries and annotations on an analyzed DS program (run as -postScript by tools/re/re.py).
// Arguments: <output file> then "@<command file>" (one command per line), or commands separated by
// ";" arguments:
//   decompile <addr|name>...      C for each function (created when an address has none)
//   disasm <addr> [count]         instructions from an address (default 40)
//   xrefs <addr|name>             who references it; literal-pool words are followed to their loads
//   calls <addr|name>             callers and callees of a function
//   funcs [start] [end]           functions in a range (default all), with sizes
//   find <hex bytes, ?? = any>    where a byte pattern occurs, with the function around each hit
//   rename <addr> <name>          name a function (or label) - findings live in the project
//   comment <addr> <text...>      plate comment
//   info                          memory blocks and counts
// Addresses: hex ("0x0204a1c0", "0204a1c0"), overlay-qualified ("ov_012::02150000"); bit 0 set = Thumb.
//@category NDS
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.SourceType;

public class NdsQuery extends GhidraScript {

    private final StringBuilder out = new StringBuilder();
    private DecompInterface decompiler;

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        List<List<String>> commands = new ArrayList<>();
        if (args.length == 2 && args[1].startsWith("@")) {
            // a command file, one command per line (what re.py passes: no shell quoting to survive)
            for (String line : Files.readAllLines(Path.of(args[1].substring(1)))) {
                line = line.strip();
                if (!line.isEmpty() && !line.startsWith("#")) {
                    commands.add(new ArrayList<>(List.of(line.split("\\s+"))));
                }
            }
        }
        else {
            List<String> current = new ArrayList<>();
            for (int i = 1; i < args.length; i++) {
                if (args[i].equals(";")) {
                    commands.add(current);
                    current = new ArrayList<>();
                }
                else {
                    current.add(args[i]);
                }
            }
            commands.add(current);
        }

        for (List<String> command : commands) {
            if (command.isEmpty()) {
                continue;
            }
            out.append("### ").append(String.join(" ", command)).append('\n');
            try {
                runCommand(command.get(0), command.subList(1, command.size()));
            }
            catch (Exception e) {
                out.append("error: ").append(e).append('\n');
            }
            out.append('\n');
        }
        if (decompiler != null) {
            decompiler.dispose();
        }
        Files.writeString(Path.of(args[0]), out.toString());
    }

    private void runCommand(String name, List<String> a) throws Exception {
        switch (name) {
            case "decompile" -> {
                for (String target : a) {
                    decompile(target);
                }
            }
            case "disasm" -> disasm(resolveAddress(a.get(0)), a.size() > 1 ? Integer.parseInt(a.get(1)) : 40);
            case "xrefs" -> xrefs(a.get(0));
            case "calls" -> calls(a.get(0));
            case "funcs" -> funcs(a.size() > 0 ? resolveAddress(a.get(0)) : null, a.size() > 1 ? resolveAddress(a.get(1)) : null);
            case "find" -> findPattern(String.join("", a));
            case "rename" -> rename(a.get(0), a.get(1));
            case "comment" -> {
                Address address = resolveAddress(a.get(0));
                setPlateComment(address, String.join(" ", a.subList(1, a.size())));
                out.append("commented ").append(address).append('\n');
            }
            case "info" -> info();
            default -> out.append("unknown command ").append(name).append('\n');
        }
    }

    // ---- targets ----------------------------------------------------------------------------

    private boolean isThumbTarget(String text) {
        String t = text.contains("::") ? text.substring(text.indexOf("::") + 2) : text;
        t = t.toLowerCase().replace("0x", "");
        if (!t.matches("[0-9a-f]+")) {
            return false;
        }
        return (Long.parseLong(t, 16) & 1) != 0;
    }

    private Address resolveAddress(String text) {
        List<Function> named = getGlobalFunctions(text);
        if (!named.isEmpty()) {
            return named.get(0).getEntryPoint();
        }
        String spec = text;
        String space = "";
        if (text.contains("::")) {
            space = text.substring(0, text.indexOf("::") + 2);
            spec = text.substring(text.indexOf("::") + 2);
        }
        spec = spec.toLowerCase().replace("0x", "");
        long value = Long.parseLong(spec, 16) & ~1L;
        Address address = currentProgram.getAddressFactory().getAddress(space + Long.toHexString(value));
        if (address == null) {
            throw new IllegalArgumentException("no address " + text);
        }
        return address;
    }

    private Function functionFor(String text, boolean create) throws Exception {
        Address address = resolveAddress(text);
        Function function = getFunctionContaining(address);
        if (function == null && create) {
            Register tmode = currentProgram.getRegister("TMode");
            if (tmode != null && isThumbTarget(text)) {
                currentProgram.getProgramContext().setValue(tmode, address, address, BigInteger.ONE);
            }
            disassemble(address);
            function = createFunction(address, null);
        }
        return function;
    }

    private String describe(Address address) {
        Function function = getFunctionContaining(address);
        String where = function == null ? "(no function)" : function.getName() + "+" + address.subtract(function.getEntryPoint());
        MemoryBlock block = getMemoryBlock(address);
        return address + " " + where + (block == null ? "" : " [" + block.getName() + "]");
    }

    // ---- commands ---------------------------------------------------------------------------

    private void decompile(String target) throws Exception {
        Function function = functionFor(target, true);
        if (function == null) {
            out.append("no function at ").append(target).append('\n');
            return;
        }
        if (decompiler == null) {
            decompiler = new DecompInterface();
            decompiler.openProgram(currentProgram);
        }
        DecompileResults results = decompiler.decompileFunction(function, 120, monitor);
        out.append("// ").append(function.getName()).append(" @ ").append(function.getEntryPoint())
            .append(" (").append(function.getBody().getNumAddresses()).append(" bytes)\n");
        if (results.decompileCompleted()) {
            out.append(results.getDecompiledFunction().getC());
        }
        else {
            out.append("decompile failed: ").append(results.getErrorMessage()).append('\n');
        }
    }

    private void disasm(Address start, int count) {
        Instruction instruction = getInstructionAt(start);
        if (instruction == null) {
            disassemble(start);
            instruction = getInstructionAt(start);
        }
        for (int i = 0; i < count && instruction != null; i++) {
            out.append(instruction.getAddress()).append("  ").append(instruction).append('\n');
            instruction = instruction.getNext();
        }
    }

    private void xrefs(String target) throws Exception {
        Address address = resolveAddress(target);
        Function function = getFunctionAt(address);
        out.append("references to ").append(describe(address)).append('\n');
        for (Reference ref : getReferencesTo(address)) {
            Address from = ref.getFromAddress();
            out.append("  ").append(ref.getReferenceType()).append(" from ").append(describe(from)).append('\n');
            // ARM loads constants from literal pools: name the instructions that load this pool word
            if (getInstructionAt(from) == null) {
                for (Reference load : getReferencesTo(from)) {
                    out.append("    loaded by ").append(describe(load.getFromAddress())).append('\n');
                }
            }
        }
        if (function != null) {
            out.append("callers: ").append(function.getCallingFunctions(monitor).size()).append('\n');
        }
    }

    private void calls(String target) throws Exception {
        Function function = functionFor(target, true);
        if (function == null) {
            out.append("no function at ").append(target).append('\n');
            return;
        }
        Set<Function> callers = function.getCallingFunctions(monitor);
        Set<Function> callees = function.getCalledFunctions(monitor);
        out.append(function.getName()).append(" @ ").append(function.getEntryPoint()).append('\n');
        out.append("callers (").append(callers.size()).append("):\n");
        for (Function f : callers) {
            out.append("  ").append(f.getName()).append(" @ ").append(f.getEntryPoint()).append('\n');
        }
        out.append("callees (").append(callees.size()).append("):\n");
        for (Function f : callees) {
            out.append("  ").append(f.getName()).append(" @ ").append(f.getEntryPoint()).append('\n');
        }
    }

    private void funcs(Address start, Address end) {
        int count = 0;
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            Address entry = f.getEntryPoint();
            if (start != null && entry.compareTo(start) < 0) {
                continue;
            }
            if (end != null && entry.compareTo(end) >= 0) {
                continue;
            }
            out.append(entry).append("  ").append(f.getName()).append("  ").append(f.getBody().getNumAddresses()).append('\n');
            if (++count >= 5000) {
                out.append("(stopped at 5000)\n");
                break;
            }
        }
        out.append(count).append(" functions\n");
    }

    private void findPattern(String pattern) throws Exception {
        String hex = pattern.replace(" ", "").replace(",", "");
        int length = hex.length() / 2;
        byte[] bytes = new byte[length];
        byte[] mask = new byte[length];
        for (int i = 0; i < length; i++) {
            String pair = hex.substring(i * 2, i * 2 + 2);
            if (pair.equals("??")) {
                continue;
            }
            bytes[i] = (byte) Integer.parseInt(pair, 16);
            mask[i] = (byte) 0xFF;
        }
        int hits = 0;
        for (MemoryBlock block : currentProgram.getMemory().getBlocks()) {
            if (!block.isInitialized()) {
                continue;
            }
            Address at = block.getStart();
            while (at != null && hits < 500) {
                Address hit = currentProgram.getMemory().findBytes(at, block.getEnd(), bytes, mask, true, monitor);
                if (hit == null) {
                    break;
                }
                out.append("  ").append(describe(hit)).append('\n');
                hits++;
                at = hit.next();
            }
        }
        out.append(hits).append(" hits\n");
    }

    private void rename(String target, String name) throws Exception {
        Function function = functionFor(target, true);
        if (function != null && function.getEntryPoint().equals(resolveAddress(target))) {
            function.setName(name, SourceType.USER_DEFINED);
            out.append("function ").append(function.getEntryPoint()).append(" = ").append(name).append('\n');
        }
        else {
            createLabel(resolveAddress(target), name, true, SourceType.USER_DEFINED);
            out.append("label ").append(target).append(" = ").append(name).append('\n');
        }
    }

    private void info() {
        for (MemoryBlock block : currentProgram.getMemory().getBlocks()) {
            out.append(block.getName()).append("  ").append(block.getStart()).append("-").append(block.getEnd())
                .append(block.isInitialized() ? "" : " (uninitialized)").append(block.isOverlay() ? " (overlay)" : "").append('\n');
        }
        out.append(currentProgram.getFunctionManager().getFunctionCount()).append(" functions\n");
    }
}
