/* Device status word layout, consumed by examples/status_report.py.
 *
 * The file deliberately avoids preprocessor directives so pycparser can
 * parse it directly.  It exercises mixed signed/unsigned arithmetic,
 * casts, bitwise NOT and shifts; a few expressions are implementation
 * defined or undefined so the report can point at their locations.
 */

/* Mutually exclusive device states (a status word holds exactly one). */
enum DevState {
    DEV_STATE_IDLE    = 0,
    DEV_STATE_RUNNING = 1,
    DEV_STATE_ERROR   = 2,
    DEV_STATE_UNKNOWN = 2
};

/* State classes built from shifts and earlier enumerators. */
enum DevClass {
    DEV_CLASS_BASE   = 1 << 4,
    DEV_CLASS_EXT    = DEV_CLASS_BASE | (1 << 5),
    DEV_CLASS_LEGACY = DEV_CLASS_EXT
};

/* Single-bit flags. */
static const unsigned int DEV_FLAG_READY = 1u << 0;
static const unsigned int DEV_FLAG_BUSY  = 1u << 1;
static const unsigned int DEV_FLAG_FAULT = 1u << 2;

/* Combined flag: hits together with the single flags above. */
static const unsigned int DEV_FLAG_PAIR = DEV_FLAG_READY | DEV_FLAG_BUSY;

/* Bitwise NOT followed by a shift: low 16 bits set. */
static const unsigned int DEV_FLAG_LOW16 = (unsigned int)~0u >> 16;

/* Mixed signed/unsigned arithmetic: -1 converts to unsigned, result 0. */
static const unsigned int DEV_FLAG_MIXED = 1u + -1;

/* Implementation defined: 2**31 is not representable in int. */
static const int DEV_FLAG_SIGNBIT = (int)(1u << 31);

/* Implementation defined: arithmetic right shift of a negative value. */
static const int DEV_FLAG_ARITH = -8 >> 1;

/* Undefined on a 32-bit target (shift count >= width of long), fine on
 * a 64-bit LP64 target. */
static const unsigned long DEV_FLAG_HIGH = 1UL << 40;

/* Undefined on every supported target: shift count >= width of int. */
static const unsigned int DEV_FLAG_TOOWIDE = 1u << 40;
