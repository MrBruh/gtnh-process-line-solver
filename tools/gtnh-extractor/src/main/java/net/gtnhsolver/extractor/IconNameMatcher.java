package net.gtnhsolver.extractor;

import java.util.Collections;
import java.util.LinkedHashMap;
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
 * Shape A is {@code BlockGTCasingsTT.registerBlockIcons} and friends; shape B is
 * {@code TTMultiblockBase.registerIcons}, whose two statics are the overlay half of every tectech
 * controller hull; shape C is {@code BlockCasingSpaceElevator}, which writes 4 of its 5 icons that
 * way. All three were read out of real bytes before being written here, not guessed - C was found
 * by disassembling, after the first two had been implemented from source.
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
 */
final class IconNameMatcher {

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
            // A class we cannot read is a class we learned nothing from. Same as no match.
            return Collections.emptyMap();
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

        IconMethodVisitor(Map<String, String> found) {
            super(Opcodes.ASM5);
            this.found = found;
        }

        private void reset() {
            pendingLiteral = null;
            consumedByAcceptedCall = false;
            pendingArrayField = null;
            pendingIndex = -1;
        }

        @Override
        public void visitLdcInsn(Object value) {
            // A second literal before the field write means we are not in a shape we understand.
            pendingLiteral = value instanceof String && !consumedByAcceptedCall ? (String) value : null;
            consumedByAcceptedCall = false;
        }

        @Override
        public void visitMethodInsn(int opcode, String owner, String name, String desc, boolean itf) {
            if (pendingLiteral == null) {
                reset();
                return;
            }
            boolean shapeA = opcode == Opcodes.INVOKEINTERFACE && isRegisterIcon(name)
                && REGISTER_ICON_DESC.equals(desc);
            boolean shapeB = opcode == Opcodes.INVOKESPECIAL && "<init>".equals(name)
                && "(Ljava/lang/String;)V".equals(desc) && owner.endsWith(CUSTOM_ICON_SUFFIX);
            if (shapeA || shapeB) {
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
            if (opcode != Opcodes.DUP) { // DUP sits between NEW and the literal in shape B
                reset();
            }
        }

        @Override
        public void visitVarInsn(int opcode, int var) {
            // ALOAD of the registry sits between the literal and the call in shape A, so a load
            // alone is not disqualifying; anything storing into a local IS, since the literal then
            // reaches the field by a path this does not model.
            if (opcode >= Opcodes.ISTORE) {
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
            if (opcode != Opcodes.NEW && opcode != Opcodes.CHECKCAST) {
                reset();
            }
        }
    }
}
