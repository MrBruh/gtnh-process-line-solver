package net.gtnhsolver.extractor;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * Recover icon names from a class's <b>unstripped</b> bytes (GitHub #98).
 *
 * <p>
 * FML's {@code SideTransformer} rewrites bytes on their way into memory, so reflection sees a class
 * with every {@code @SideOnly(CLIENT)} member deleted. The {@code .class} in the jar is never
 * touched, and {@code LaunchClassLoader.getClassBytes} hands back the pre-transform copy. So where
 * a name exists only as a string literal inside a method the server deleted, reading the bytes is
 * not the preferred route: it is the only one. A stub {@code IIconRegister} cannot work, because
 * that interface is itself client-only and a class implementing it will not load (verified over a
 * full run: found on 806 blocks, threw on all 806).
 *
 * <p>
 * This class is deliberately <b>pure</b>: bytes in, {@code {field name -> icon name}} out, no
 * Minecraft classes, no reflection, no side effects. That is what lets it be unit tested against
 * real class files without booting a server, which is the whole reason the risky part of this
 * extractor can be tested at all.
 *
 * <h2>The two shapes, and why only two</h2>
 *
 * <pre>
 * A: registerIcon into an IIcon field          B: a CustomIcon constructed into a holder
 *    ALOAD 1                                      NEW    .../CustomIcon
 *    LDC "gregtech:iconsets/EM_POWER"             DUP
 *    INVOKEINTERFACE registerIcon                 LDC    "iconsets/EM_CONTROLLER_ACTIVE"
 *    PUTSTATIC eM0 : IIcon                        INVOKESPECIAL CustomIcon.&lt;init&gt;(String)
 *                                                 PUTSTATIC ScreenON : CustomIcon
 * </pre>
 *
 * <pre>
 * C: registerIcon into an element of an IIcon[] field, at a constant index
 *    GETSTATIC IconSECasing1 : [Lnet/minecraft/util/IIcon;
 *    ICONST_0
 *    ALOAD 1
 *    LDC "gtnhintergalactic:spaceElevator/SupportStructure"
 *    INVOKEINTERFACE registerIcon
 *    AASTORE                                   -&gt; keyed "IconSECasing1[0]"
 * </pre>
 *
 * <pre>
 * B': the same as B, as GT 2.9 spells it after BlockIcons stopped being an enum
 *    LDC "iconsets/EM_CONTROLLER"
 *    INVOKESTATIC Textures$BlockIcons.custom (String)IIconContainer
 *    PUTSTATIC ScreenOFF : IIconContainer
 * </pre>
 *
 * <pre>
 * B'': the same factory with its domain passed apart, as GT 5.09.54.133 spells it
 *    GETSTATIC Mods.GregTech : Mods        (or: LDC "gregtech")
 *    GETFIELD  Mods.resourceDomain : String
 *    LDC "iconsets/EM_CONTROLLER"
 *    INVOKESTATIC Textures$BlockIcons.custom (String, String)IIconContainer
 *    PUTSTATIC ScreenOFF : IIconContainer
 * </pre>
 *
 * Shape A is {@code BlockGTCasingsTT.registerBlockIcons} and friends; shape B is
 * {@code TTMultiblockBase.registerIcons}, whose two statics are the overlay half of every tectech
 * controller hull; shape C is {@code BlockCasingSpaceElevator}, which writes 4 of its 5 icons that
 * way. All three were read out of real bytes before being written here, not guessed - C was found
 * by disassembling, after the first two had been implemented from source. B' and B'' are shape B's
 * {@code TTMultiblockBase.registerIcons} again, at GT 5.09.54.20 and 5.09.54.133.
 *
 * <p>
 * <b>Shape C is not the array case the notes warn against.</b> Non-negotiable 3 in
 * {@code texture-resolution.md} says an array arm must delegate to a runtime field read rather than
 * infer contents, and for its own example it is right: {@code BlockCasingsNH} indexes the
 * <i>shared</i> {@code MACHINECASINGS_*} arrays, whose elements are filled somewhere else entirely
 * and cannot be known from the call site. Here the array, the constant index and the literal are
 * one unbroken sequence in one method, so the index is read rather than assumed. A computed index
 * is not matched at all.
 *
 * <p>
 * <b>Anything else is recorded as nothing at all.</b> A shape this does not recognise contributes
 * no entry, so the caller keeps its existing gap. That is non-negotiable and is why the state
 * machine below resets on any instruction that is not part of an accepted sequence: a loose matcher
 * that "usually" pairs the nearest literal with the nearest field would emit confident wrong
 * sprites, which is the one failure mode nothing downstream can detect (see #130, and
 * {@code docs/dataset-extraction/texture-resolution.md} trap 5).
 *
 * <p>
 * <b>The literals are not normalised here.</b> Shape A carries its domain ({@code "gregtech:..."}),
 * shape B does not ({@code "iconsets/..."}), and re-prefixing either one is a bug that has already
 * cost this project 46 unfetchable asset paths. The raw literal is returned exactly as written and
 * the caller decides, which for shape B means handing it to {@code CustomIcon}, whose own
 * constructor applies {@code GregTech.getResourcePath} to a bare name.
 *
 * <p>
 * Shape B'' is the one case with two strings, and it is folded into the spelling the one-argument
 * factory reads, so the caller still has a single name to hand on: the path literal as written when
 * the domain is GregTech's own (a bare name already means {@code gregtech} to both factories), else
 * {@code domain:path}. The domain is only ever taken as written: an LDC, or {@code Mods.GregTech}'s
 * {@code resourceDomain}, which is {@code "gregtech"}. Any other {@code Mods} constant's domain is its
 * mod id lower-cased, which bytes alone cannot say, so it yields nothing rather than a guess. Every
 * {@code custom(domain, path)} call in the allowlisted classes at 5.09.54.133 passes
 * {@code Mods.GregTech.resourceDomain}.
 */
final class IconNameMatcher {

    /** The bytes could not be parsed at all, which is a different fact from finding no names. */
    static final class UnreadableClassException extends RuntimeException {

        private static final long serialVersionUID = 1L;

        UnreadableClassException(Throwable cause) {
            super(cause);
        }
    }


    /**
     * The interface method shape A calls, under both spellings it appears in.
     *
     * <p>
     * The monorepo jar is not uniformly deobfuscated: {@code BlockGTCasingsTT} calls
     * {@code registerIcon} while {@code BlockCasingSpaceElevator} calls the SRG name
     * {@code func_94245_a}, and a matcher that knows only one silently skips the other's whole mod.
     * {@code TextureDumper.GET_ICON_NAMES} carries the same pair for the same reason.
     */
    private static final String[] REGISTER_ICON = { "registerIcon", "func_94245_a" };

    /** The descriptor of that call, pinned so an unrelated one-string method cannot match it. */
    private static final String REGISTER_ICON_DESC = "(Ljava/lang/String;)Lnet/minecraft/util/IIcon;";

    /** Simple name of the icon-holder classes shape B constructs; matched on the internal name. */
    private static final String CUSTOM_ICON_SUFFIX = "CustomIcon";

    /**
     * Shape B': the static factory 2.9 replaced the {@code CustomIcon} constructor with.
     *
     * <p>
     * GT 2.9 refactored {@code Textures.BlockIcons} from an enum into a class, and the holder that
     * went with it: {@code new CustomIcon(name)} became {@code Textures.BlockIcons.custom(name)},
     * returning the {@code IIconContainer} interface rather than a concrete class. Shape B keys on
     * an {@code INVOKESPECIAL} constructor, so on 2.9 it matches nothing - and "matches nothing" is
     * silent. Left alone, every tectech controller overlay would quietly regress on the newer pack
     * while the run still reported success.
     */
    private static final String CUSTOM_FACTORY = "custom";

    /** The factory's return type, pinned so an unrelated {@code custom(String)} cannot match. */
    private static final String ICON_CONTAINER_DESC = "(Ljava/lang/String;)Lgregtech/api/interfaces/IIconContainer;";

    /**
     * Shape B'': the two-argument {@code custom(domain, path)} GT 5.09.54.133 moved its callers to, with the
     * same pinned return type. Its one-argument sibling survives as a deprecated stub, so the caller can
     * still inject through it.
     */
    private static final String DOMAIN_ICON_CONTAINER_DESC = "(Ljava/lang/String;Ljava/lang/String;)"
        + "Lgregtech/api/interfaces/IIconContainer;";

    /** GT's mod enum, whose constants carry a {@code resourceDomain} shape B'' passes as its domain. */
    private static final String MODS_OWNER = "gregtech/api/enums/Mods";

    /** The one {@code Mods} constant whose domain is known without running it: {@code ID.toLowerCase()}. */
    private static final String GREGTECH_MOD = "GregTech";

    /** That constant's {@code resourceDomain}, and the domain a bare icon name already means. */
    private static final String GREGTECH_DOMAIN = "gregtech";

    /** Stands for a {@code Mods} domain this class cannot name, so the call it feeds yields nothing. */
    private static final String UNKNOWN_DOMAIN = "";

    /**
     * The methods worth walking; everything else in the class is skipped outright.
     * {@code func_149651_a} is {@code registerBlockIcons} under SRG naming - see
     * {@link #REGISTER_ICON} for why both spellings have to be listed.
     */
    private static final String[] ICON_METHODS = { "registerBlockIcons", "registerIcons", "func_149651_a" };

    private IconNameMatcher() {}

    /**
     * The {@code {static field name -> raw icon name}} pairs this class's icon registration writes.
     *
     * <p>
     * Empty when the class registers nothing, when its shapes are unrecognised, or when the bytes
     * cannot be parsed. Every one of those is "we learned nothing here", which leaves the caller's
     * gap in place - never a partial or inferred answer.
     */
    static Map<String, String> iconNames(byte[] classBytes) {
        if (classBytes == null || classBytes.length == 0) {
            return Collections.emptyMap();
        }
        Map<String, String> found = new LinkedHashMap<>();
        try {
            new ClassReader(classBytes).accept(new IconClassVisitor(found), ClassReader.SKIP_FRAMES);
        } catch (RuntimeException e) {
            // "Could not read the class" is NOT the same fact as "read it and it had no names", and
            // collapsing the two is how a whole mod goes missing quietly. ASM 5.0.3 (what
            // launchwrapper puts on the classpath) refuses anything past Java 8 bytecode with a bare
            // IllegalArgumentException, which is exactly what a multi-release jar's versioned
            // overlay looks like. The caller logs this; it must never read as an empty class.
            throw new UnreadableClassException(e);
        }
        return found;
    }

    private static boolean isRegisterIcon(String name) {
        for (String candidate : REGISTER_ICON) {
            if (candidate.equals(name)) {
                return true;
            }
        }
        return false;
    }

    private static boolean isIconMethod(String name) {
        for (String candidate : ICON_METHODS) {
            if (candidate.equals(name)) {
                return true;
            }
        }
        return false;
    }

    private static final class IconClassVisitor extends ClassVisitor {

        private final Map<String, String> found;

        IconClassVisitor(Map<String, String> found) {
            super(Opcodes.ASM5);
            this.found = found;
        }

        @Override
        public MethodVisitor visitMethod(int access, String name, String desc, String sig, String[] ex) {
            return isIconMethod(name) ? new IconMethodVisitor(found) : null;
        }
    }

    /**
     * Matches the two accepted sequences and nothing else.
     *
     * <p>
     * The state is one pending literal plus whether the call that consumed it was one we accept.
     * Both are cleared by anything unexpected, so a sequence that merely resembles an accepted one
     * yields no entry rather than a plausible pairing.
     */
    private static final class IconMethodVisitor extends MethodVisitor {

        private final Map<String, String> found;
        private String pendingLiteral;
        private boolean consumedByAcceptedCall;
        /** Shape C only: the array field being filled, and the constant index into it. */
        private String pendingArrayField;
        private int pendingIndex = -1;
        /**
         * Shape B'' only: the domain argument read ahead of the path literal ({@link #UNKNOWN_DOMAIN} for a
         * {@code Mods} constant this class cannot name), and the {@code Mods} constant loaded while its
         * {@code resourceDomain} read is still to come.
         */
        private String pendingDomain;
        private String pendingModConstant;

        IconMethodVisitor(Map<String, String> found) {
            super(Opcodes.ASM5);
            this.found = found;
        }

        private void reset() {
            pendingLiteral = null;
            consumedByAcceptedCall = false;
            pendingArrayField = null;
            pendingIndex = -1;
            pendingDomain = null;
            pendingModConstant = null;
        }

        @Override
        public void visitLdcInsn(Object value) {
            boolean usable = value instanceof String && !consumedByAcceptedCall && pendingModConstant == null;
            consumedByAcceptedCall = false;
            pendingModConstant = null; // a Mods constant goes straight to its resourceDomain, or not at all
            if (!usable) {
                pendingLiteral = null;
                pendingDomain = null;
                return;
            }
            if (pendingLiteral == null) {
                pendingLiteral = (String) value;
                return;
            }
            if (pendingDomain == null && pendingArrayField == null) {
                // Shape B'': a literal straight after another is the path, and the one before its domain.
                pendingDomain = pendingLiteral;
                pendingLiteral = (String) value;
                return;
            }
            // A third literal before the field write means we are not in a shape we understand.
            pendingLiteral = null;
            pendingDomain = null;
        }

        @Override
        public void visitMethodInsn(int opcode, String owner, String name, String desc, boolean itf) {
            if (pendingLiteral == null) {
                reset();
                return;
            }
            // Every one-string shape refuses a pending domain: that literal went somewhere we do not model.
            boolean bare = pendingDomain == null;
            boolean shapeA = bare && opcode == Opcodes.INVOKEINTERFACE && isRegisterIcon(name)
                && REGISTER_ICON_DESC.equals(desc);
            boolean shapeB = bare && opcode == Opcodes.INVOKESPECIAL && "<init>".equals(name)
                && "(Ljava/lang/String;)V".equals(desc) && owner.endsWith(CUSTOM_ICON_SUFFIX);
            // B' is the same fact as B, spelled the way 2.9 spells it. See CUSTOM_FACTORY.
            boolean shapeBPrime = bare && opcode == Opcodes.INVOKESTATIC && CUSTOM_FACTORY.equals(name)
                && ICON_CONTAINER_DESC.equals(desc);
            // B'' is B' with its domain passed apart. See DOMAIN_ICON_CONTAINER_DESC.
            boolean shapeBDoublePrime = !bare && opcode == Opcodes.INVOKESTATIC && CUSTOM_FACTORY.equals(name)
                && DOMAIN_ICON_CONTAINER_DESC.equals(desc);
            if (shapeBDoublePrime) {
                String joined = withDomain(pendingDomain, pendingLiteral);
                if (joined == null) {
                    reset(); // a domain we cannot name is not one we guess at
                    return;
                }
                pendingLiteral = joined;
                pendingDomain = null;
                consumedByAcceptedCall = true;
            } else if (shapeA || shapeB || shapeBPrime) {
                consumedByAcceptedCall = true;
            } else {
                reset(); // the literal went somewhere we do not model; forget it
            }
        }

        @Override
        public void visitFieldInsn(int opcode, String owner, String name, String desc) {
            if (opcode == Opcodes.PUTSTATIC) {
                if (pendingLiteral != null && consumedByAcceptedCall) {
                    found.put(name, pendingLiteral);
                }
                reset(); // one literal feeds one field; never carry it to a second
                return;
            }
            if (opcode == Opcodes.GETSTATIC && desc.startsWith("[")) {
                reset(); // shape C opens here, so any earlier half-sequence is abandoned
                pendingArrayField = name;
                return;
            }
            if (opcode == Opcodes.GETSTATIC && MODS_OWNER.equals(owner) && ("L" + MODS_OWNER + ";").equals(desc)) {
                reset(); // shape B'' opens here, with the domain's mod
                pendingModConstant = name;
                return;
            }
            if (opcode == Opcodes.GETFIELD && pendingModConstant != null && MODS_OWNER.equals(owner)
                && "resourceDomain".equals(name) && "Ljava/lang/String;".equals(desc)) {
                pendingDomain = GREGTECH_MOD.equals(pendingModConstant) ? GREGTECH_DOMAIN : UNKNOWN_DOMAIN;
                pendingModConstant = null;
                return;
            }
            reset();
        }

        @Override
        public void visitInsn(int opcode) {
            if (opcode >= Opcodes.ICONST_0 && opcode <= Opcodes.ICONST_5) {
                if (pendingArrayField == null) {
                    reset(); // a constant outside an array sequence is not a shape we model
                } else {
                    pendingIndex = opcode - Opcodes.ICONST_0;
                }
                return;
            }
            if (opcode == Opcodes.AASTORE) {
                if (pendingArrayField != null && pendingIndex >= 0 && pendingLiteral != null
                    && consumedByAcceptedCall) {
                    found.put(pendingArrayField + "[" + pendingIndex + "]", pendingLiteral);
                }
                reset();
                return;
            }
            // DUP sits between NEW and the literal in shape B, but never inside shape B''.
            if (opcode != Opcodes.DUP || pendingModConstant != null) {
                reset();
            }
        }

        @Override
        public void visitVarInsn(int opcode, int var) {
            // ALOAD of the registry sits between the literal and the call in shape A, so a load
            // alone is not disqualifying; anything storing into a local IS, since the literal then
            // reaches the field by a path this does not model. Nothing sits between a Mods constant
            // and its resourceDomain read, so there even a load is.
            if (opcode >= Opcodes.ISTORE || pendingModConstant != null) {
                reset();
            }
        }

        @Override
        public void visitJumpInsn(int opcode, org.objectweb.asm.Label label) {
            reset(); // a branch between literal and field write is a shape we do not understand
        }

        @Override
        public void visitIntInsn(int opcode, int operand) {
            // BIPUSH/SIPUSH is how an array index past 5 is pushed; it is still a constant.
            if (pendingArrayField != null && (opcode == Opcodes.BIPUSH || opcode == Opcodes.SIPUSH)) {
                pendingIndex = operand;
                return;
            }
            reset();
        }

        @Override
        public void visitTypeInsn(int opcode, String type) {
            // NEW of the holder precedes the literal in shape B; it must not clear a pending one.
            if ((opcode != Opcodes.NEW && opcode != Opcodes.CHECKCAST) || pendingModConstant != null) {
                reset();
            }
        }
    }

    /**
     * The {@code (domain, path)} of shape B'' as the one name the one-argument factory reads, or null when
     * the domain cannot be named or the path already carries one of its own (the two-argument factory takes
     * a colon-free path, so a colon there is a shape we do not model).
     */
    private static String withDomain(String domain, String path) {
        if (domain == null || domain.isEmpty() || path.indexOf(':') >= 0) {
            return null;
        }
        // ResourceLocation lower-cases a domain, so "GregTech" is GregTech's domain too.
        return GREGTECH_DOMAIN.equals(domain.toLowerCase(Locale.ROOT)) ? path : domain + ":" + path;
    }
}
