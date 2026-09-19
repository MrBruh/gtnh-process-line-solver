package net.gtnhsolver.extractor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Map;

import org.junit.Test;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * Unit tests for the ASM icon-name matcher (GitHub #98).
 *
 * <p>
 * <b>This suite is deliberately separate from the Python one.</b> It runs under
 * {@code ./gradlew test} inside {@code tools/gtnh-extractor} and is not wired into {@code pytest}
 * or the repo's CI gate: the extractor is a local-only tool whose real gate is a dump run, and the
 * Python suite must not acquire a dependency on a Forge toolchain to stay fast.
 *
 * <p>
 * Two kinds of case, because each catches what the other cannot. <b>Synthetic bytes</b> (emitted
 * with ASM right here) pin the exact instruction sequences, including the near-misses that a loose
 * matcher would pair up wrongly and that no real class happens to contain. <b>Real bytes</b>, read
 * out of the pinned GT5-Unofficial jar on the test classpath, prove the shapes were read correctly
 * from the actual source rather than from an idea of it.
 */
public class IconNameMatcherTest {

    // ------------------------------------------------------------------ real bytes, pinned jar

    @Test
    public void reads_the_real_tectech_casing_block() throws IOException {
        byte[] bytes = classBytes("tectech/thing/casing/BlockGTCasingsTT.class");
        assertNotNull("BlockGTCasingsTT must be on the test classpath", bytes);

        Map<String, String> names = IconNameMatcher.iconNames(bytes);

        // Shape A, verbatim from registerBlockIcons. The literal already carries its domain and
        // must come back un-prefixed; re-prefixing it is the bug that cost 46 asset paths before.
        assertEquals("gregtech:iconsets/EM_POWER", names.get("eM0"));
        assertEquals("gregtech:iconsets/EM_PC_NONSIDE", names.get("eM1"));
        assertEquals("gregtech:iconsets/EM_PC", names.get("eM1s"));
        assertEquals("gregtech:iconsets/EM_CASING", names.get("eM4"));
        assertTrue("every eM field should resolve, not just the first", names.size() >= 15);
    }

    @Test
    public void reads_the_real_tectech_controller_overlays() throws IOException {
        byte[] bytes = classBytes("tectech/thing/metaTileEntity/multi/base/TTMultiblockBase.class");
        assertNotNull("TTMultiblockBase must be on the test classpath", bytes);

        Map<String, String> names = IconNameMatcher.iconNames(bytes);

        // Shape B, from an @SideOnly method: the annotation deletes the method at runtime but
        // never touches the bytes, which is the entire premise of this matcher.
        assertEquals("iconsets/EM_CONTROLLER", names.get("ScreenOFF"));
        assertEquals("iconsets/EM_CONTROLLER_ACTIVE", names.get("ScreenON"));
    }

    @Test
    public void reads_the_real_space_elevator_casing_under_srg_names() throws IOException {
        byte[] bytes = classBytes("gtnhintergalactic/block/BlockCasingSpaceElevator.class");
        assertNotNull("BlockCasingSpaceElevator must be on the test classpath", bytes);

        Map<String, String> names = IconNameMatcher.iconNames(bytes);

        // The monorepo jar is not uniformly deobfuscated: this class calls func_94245_a inside
        // func_149651_a, where the tectech one calls registerIcon inside registerBlockIcons. A
        // matcher that knew only the MCP spelling would skip this mod entirely and report it as a
        // block with no recoverable name, which is a wrong answer dressed as an honest gap.
        assertTrue("SRG-named registration must be walked too: " + names, names.size() >= 5);
        // Shape A for the one scalar field...
        assertEquals("gtnhintergalactic:spaceElevator/BaseCasing", names.get("IconSECasing0"));
        // ...and shape C for the four that land in IIcon[] fields at constant indices. Its icons
        // are not under iconsets/ either, and must not be rewritten to look as though they are.
        assertEquals("gtnhintergalactic:spaceElevator/SupportStructure", names.get("IconSECasing1[0]"));
        assertEquals("gtnhintergalactic:spaceElevator/SupportStructure_Side", names.get("IconSECasing1[1]"));
        assertEquals("gtnhintergalactic:spaceElevator/InternalStructure", names.get("IconSECasing2[0]"));
        assertEquals("gtnhintergalactic:spaceElevator/InternalStructure_Side", names.get("IconSECasing2[1]"));
    }

    @Test
    public void an_array_write_at_a_computed_index_yields_nothing() {
        // The guard that keeps shape C honest, and the line between it and the array case the
        // notes warn about: with a computed index the element cannot be known from the call site.
        Map<String, String> names = IconNameMatcher.iconNames(custom("registerBlockIcons", new Emit() {

            @Override
            public void emit(MethodVisitor mv) {
                mv.visitFieldInsn(Opcodes.GETSTATIC, "Synth", "arr", "[Lnet/minecraft/util/IIcon;");
                mv.visitVarInsn(Opcodes.ILOAD, 2); // a variable index, not a constant
                mv.visitVarInsn(Opcodes.ALOAD, 1);
                mv.visitLdcInsn("gregtech:iconsets/COMPUTED");
                mv.visitMethodInsn(Opcodes.INVOKEINTERFACE,
                    "net/minecraft/client/renderer/texture/IIconRegister", "registerIcon",
                    "(Ljava/lang/String;)Lnet/minecraft/util/IIcon;", true);
                mv.visitInsn(Opcodes.AASTORE);
            }
        }));

        assertTrue("a computed index must not be guessed at: " + names, names.isEmpty());
    }

    // --------------------------------------------------------------- synthetic bytes, exact shapes

    @Test
    public void shape_a_pairs_a_registered_icon_with_its_field() {
        Map<String, String> names = IconNameMatcher.iconNames(shapeA("eM0", "gregtech:iconsets/EM_POWER"));

        assertEquals("gregtech:iconsets/EM_POWER", names.get("eM0"));
        assertEquals(1, names.size());
    }

    @Test
    public void shape_b_pairs_a_constructed_holder_with_its_field() {
        Map<String, String> names = IconNameMatcher.iconNames(shapeB("ScreenON", "iconsets/EM_CONTROLLER_ACTIVE"));

        assertEquals("iconsets/EM_CONTROLLER_ACTIVE", names.get("ScreenON"));
        assertEquals(1, names.size());
    }

    @Test
    public void a_bare_literal_is_returned_unprefixed_and_a_qualified_one_unchanged() {
        assertEquals("iconsets/X", IconNameMatcher.iconNames(shapeB("f", "iconsets/X")).get("f"));
        assertEquals("gregtech:iconsets/X", IconNameMatcher.iconNames(shapeA("f", "gregtech:iconsets/X")).get("f"));
    }

    // ------------------------------------------------------------------------- refusing to guess

    @Test
    public void a_literal_passed_to_an_unknown_call_yields_nothing() {
        // Same literal, same field write, but the call between them is not one we model. A matcher
        // that shrugged and paired them anyway would emit a confident wrong sprite.
        Map<String, String> names = IconNameMatcher.iconNames(
            custom("registerBlockIcons", new Emit() {

                @Override
                public void emit(MethodVisitor mv) {
                    mv.visitLdcInsn("iconsets/NOT_AN_ICON_CALL");
                    mv.visitMethodInsn(Opcodes.INVOKESTATIC, "java/lang/String", "valueOf",
                        "(Ljava/lang/Object;)Ljava/lang/String;", false);
                    mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", "f", "Ljava/lang/String;");
                }
            }));

        assertTrue("an unmodelled call must contribute nothing: " + names, names.isEmpty());
    }

    @Test
    public void a_branch_between_the_literal_and_the_field_yields_nothing() {
        Map<String, String> names = IconNameMatcher.iconNames(
            custom("registerBlockIcons", new Emit() {

                @Override
                public void emit(MethodVisitor mv) {
                    mv.visitLdcInsn("gregtech:iconsets/CONDITIONAL");
                    Label skip = new Label();
                    mv.visitJumpInsn(Opcodes.GOTO, skip);
                    mv.visitLabel(skip);
                    mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", "f", "Lnet/minecraft/util/IIcon;");
                }
            }));

        assertTrue("a conditional shape is one we do not model: " + names, names.isEmpty());
    }

    @Test
    public void a_second_literal_before_the_field_write_yields_nothing() {
        Map<String, String> names = IconNameMatcher.iconNames(
            custom("registerBlockIcons", new Emit() {

                @Override
                public void emit(MethodVisitor mv) {
                    mv.visitLdcInsn("gregtech:iconsets/FIRST");
                    mv.visitLdcInsn("gregtech:iconsets/SECOND");
                    mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", "f", "Lnet/minecraft/util/IIcon;");
                }
            }));

        assertTrue("two literals and one field is ambiguous, so neither wins: " + names, names.isEmpty());
    }

    @Test
    public void a_method_that_is_not_icon_registration_is_not_walked() {
        Map<String, String> names = IconNameMatcher.iconNames(
            custom("someOtherMethod", new Emit() {

                @Override
                public void emit(MethodVisitor mv) {
                    mv.visitVarInsn(Opcodes.ALOAD, 0);
                    mv.visitLdcInsn("gregtech:iconsets/ELSEWHERE");
                    mv.visitMethodInsn(Opcodes.INVOKEINTERFACE, "net/minecraft/client/renderer/texture/IIconRegister",
                        "registerIcon", "(Ljava/lang/String;)Lnet/minecraft/util/IIcon;", true);
                    mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", "f", "Lnet/minecraft/util/IIcon;");
                }
            }));

        assertTrue("only the icon registration methods are in scope: " + names, names.isEmpty());
    }

    @Test
    public void unreadable_or_empty_bytes_yield_nothing_rather_than_throwing() {
        assertTrue(IconNameMatcher.iconNames(null).isEmpty());
        assertTrue(IconNameMatcher.iconNames(new byte[0]).isEmpty());
        assertTrue(IconNameMatcher.iconNames(new byte[] { 1, 2, 3, 4 }).isEmpty());
    }

    // ------------------------------------------------------------------------------------ helpers

    private interface Emit {

        void emit(MethodVisitor mv);
    }

    private static byte[] shapeA(final String field, final String icon) {
        return custom("registerBlockIcons", new Emit() {

            @Override
            public void emit(MethodVisitor mv) {
                mv.visitVarInsn(Opcodes.ALOAD, 1);
                mv.visitLdcInsn(icon);
                mv.visitMethodInsn(Opcodes.INVOKEINTERFACE, "net/minecraft/client/renderer/texture/IIconRegister",
                    "registerIcon", "(Ljava/lang/String;)Lnet/minecraft/util/IIcon;", true);
                mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", field, "Lnet/minecraft/util/IIcon;");
            }
        });
    }

    private static byte[] shapeB(final String field, final String icon) {
        return custom("registerIcons", new Emit() {

            @Override
            public void emit(MethodVisitor mv) {
                String owner = "gregtech/api/enums/Textures$BlockIcons$CustomIcon";
                mv.visitTypeInsn(Opcodes.NEW, owner);
                mv.visitInsn(Opcodes.DUP);
                mv.visitLdcInsn(icon);
                mv.visitMethodInsn(Opcodes.INVOKESPECIAL, owner, "<init>", "(Ljava/lang/String;)V", false);
                mv.visitFieldInsn(Opcodes.PUTSTATIC, "Synth", field, "L" + owner + ";");
            }
        });
    }

    /** A minimal class carrying one method with the given body. */
    private static byte[] custom(String methodName, Emit body) {
        ClassWriter cw = new ClassWriter(0);
        cw.visit(Opcodes.V1_6, Opcodes.ACC_PUBLIC, "Synth", null, "java/lang/Object", null);
        MethodVisitor mv = cw.visitMethod(Opcodes.ACC_PUBLIC, methodName, "(Ljava/lang/Object;)V", null, null);
        mv.visitCode();
        body.emit(mv);
        mv.visitInsn(Opcodes.RETURN);
        mv.visitMaxs(4, 4);
        mv.visitEnd();
        cw.visitEnd();
        return cw.toByteArray();
    }

    /** Read a class file off the test classpath, or null when the jar does not carry it. */
    private static byte[] classBytes(String resource) throws IOException {
        try (InputStream in = IconNameMatcherTest.class.getClassLoader()
            .getResourceAsStream(resource)) {
            if (in == null) {
                return null;
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            for (int read = in.read(buffer); read > 0; read = in.read(buffer)) {
                out.write(buffer, 0, read);
            }
            return out.toByteArray();
        }
    }
}
