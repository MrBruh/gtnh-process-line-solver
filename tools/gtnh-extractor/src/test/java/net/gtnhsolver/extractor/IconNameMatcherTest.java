package net.gtnhsolver.extractor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

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
    public void nothing_to_read_yields_nothing() {
        assertTrue(IconNameMatcher.iconNames(null).isEmpty());
        assertTrue(IconNameMatcher.iconNames(new byte[0]).isEmpty());
    }

    @Test(expected = IconNameMatcher.UnreadableClassException.class)
    public void bytes_that_will_not_parse_are_loud_rather_than_empty() {
        // "could not read it" must not arrive looking like "read it, found nothing". ASM 5.0.3
        // refuses anything past Java 8 bytecode with a bare IllegalArgumentException, which is what
        // a multi-release jar's versioned overlay produces, and swallowing that would drop a whole
        // mod's sprites from a run that still reported success.
        IconNameMatcher.iconNames(new byte[] { 1, 2, 3, 4 });
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

    /**
     * Read a class file from the pinned jar, or null when it does not carry it.
     *
     * <p>
     * <b>Deliberately NOT {@code getResourceAsStream}.</b> GT5U 2.9 ships a <b>multi-release</b>
     * jar: the base entry is Java 8 bytecode, and {@code META-INF/versions/17/} holds a Java 17
     * copy of the same class. A modern JVM's classloader prefers the versioned overlay, so a test
     * reading through the classloader gets major-61 bytes that ASM 5.0.3 refuses, while the dump
     * itself - a Forge 1.7.10 server on Java 8, reading through
     * {@code LaunchClassLoader.getClassBytes} - gets the base Java 8 entry and parses it fine.
     *
     * <p>
     * A test that reads the overlay is therefore testing bytes no dump will ever see, and it fails
     * for a reason the production path does not have. Opening the jar with {@link ZipFile}, which
     * knows nothing about multi-release versioning, pins the base entry: the same bytes the
     * extractor works on.
     */
    private static byte[] classBytes(String resource) throws IOException {
        for (String entry : System.getProperty("java.class.path").split(File.pathSeparator)) {
            if (!entry.endsWith(".jar")) {
                continue;
            }
            try (ZipFile jar = new ZipFile(entry)) {
                ZipEntry found = jar.getEntry(resource);
                if (found == null) {
                    continue;
                }
                try (InputStream in = jar.getInputStream(found)) {
                    ByteArrayOutputStream out = new ByteArrayOutputStream();
                    byte[] buffer = new byte[8192];
                    // `!= -1`, not `> 0`: a stream may legally return 0 without being at EOF, and
                    // stopping there hands ClassReader a truncated class, which it rejects with a
                    // bare IllegalArgumentException that reads exactly like "bytecode too new".
                    for (int read = in.read(buffer); read != -1; read = in.read(buffer)) {
                        out.write(buffer, 0, read);
                    }
                    return out.toByteArray();
                }
            } catch (IOException e) {
                // not a readable jar; try the next classpath entry
            }
        }
        return null;
    }
}
