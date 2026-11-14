/*
 * Representative sampling pipeline used by sampling_analysis tests.
 *
 * db_write() is the only function that writes a row.  The wrappers
 * forward and permute (channel, field, scale) before reaching it.
 * Some entry points exercise branch correlation, structs, recursion and
 * unknown callees.
 */

#define TEMP_CHANNEL 3
#define HUM_CHANNEL 4
#define TEMP_FIELD 0x10
#define HUM_FIELD 0x20
#define TEMP_SCALE 0.1
#define HUM_SCALE 0.01

struct sample {
    int channel;
    int field;
    double scale;
    int raw;
};

/* The one real write point: (channel, field, scale, raw). */
void db_write(int channel, int field, double scale, int raw)
{
    (void)channel;
    (void)field;
    (void)scale;
    (void)raw;
}

/* Layer 1: swaps the first two payload fields. */
void emit(int channel, int field, double scale, int raw)
{
    db_write(field, channel, scale, raw);
}

/* Layer 2: forwards in its own parameter order. */
void send_row(int channel, int field, double scale, int raw)
{
    emit(channel, field, scale, raw);
}

/* Layer 3: swaps channel and scale as the stack gets rebuilt. */
void dispatch(double scale, int field, int channel, int raw)
{
    send_row(channel, field, scale, raw);
}

/* Layer 4: rebuilds argument order from a scratch frame. */
void push_sample(int raw, int channel, int field, double scale)
{
    dispatch(scale, field, channel, raw);
}

/* Selects a channel/field pair and its matching scale in one branch. */
void sample_environment(int kind, int raw)
{
    if (kind == 0) {
        push_sample(raw, TEMP_CHANNEL, TEMP_FIELD, TEMP_SCALE);
    } else {
        push_sample(raw, HUM_CHANNEL, HUM_FIELD, HUM_SCALE);
    }
}

/* Same-branch selection via locals: channel and scale travel together. */
void sample_environment_local(int kind, int raw)
{
    int channel;
    int field;
    double scale;
    if (kind == 0) {
        channel = TEMP_CHANNEL;
        field = TEMP_FIELD;
        scale = TEMP_SCALE;
    } else {
        channel = HUM_CHANNEL;
        field = HUM_FIELD;
        scale = HUM_SCALE;
    }
    push_sample(raw, channel, field, scale);
}

/* Each branch independently assigns one variable.  The correlated
 * temperature pair must remain intact, and humidity must not be paired
 * with the temperature scale. */
void sample_shared(int use_humidity, int raw)
{
    int channel = TEMP_CHANNEL;
    double scale = TEMP_SCALE;
    if (use_humidity) {
        channel = HUM_CHANNEL;
    }
    if (use_humidity) {
        scale = HUM_SCALE;
    }
    push_sample(raw, channel, TEMP_FIELD, scale);
}

/* Struct-carried payload: member order is not the argument order. */
void sample_struct(int kind, int raw)
{
    struct sample s;
    if (kind == 0) {
        s.channel = TEMP_CHANNEL;
        s.field = TEMP_FIELD;
        s.scale = TEMP_SCALE;
    } else {
        s.channel = HUM_CHANNEL;
        s.field = HUM_FIELD;
        s.scale = HUM_SCALE;
    }
    s.raw = raw;
    emit(s.channel, s.field, s.scale, s.raw);
}

/* Recursive helper: recursion truncates only the helper path. */
int accumulate(int depth, int value)
{
    if (depth == 0) {
        return value;
    }
    return accumulate(depth - 1, value + 1);
}

void sample_recursive(int raw)
{
    int value = accumulate(4, raw);
    db_write(TEMP_CHANNEL, TEMP_FIELD, TEMP_SCALE, value);
    push_sample(raw, TEMP_CHANNEL, TEMP_FIELD, TEMP_SCALE);
}

/* Unknown external call: the path using its result is truncated, the
 * direct write below still reaches db_write. */
extern int read_adc(int pin);

void sample_unknown(int raw)
{
    int value = read_adc(1);
    db_write(HUM_CHANNEL, HUM_FIELD, HUM_SCALE, value);
    push_sample(raw, HUM_CHANNEL, HUM_FIELD, HUM_SCALE);
}

/* Switch dispatch over constant modes. */
void sample_switch(int mode, int raw)
{
    switch (mode) {
    case 0:
        push_sample(raw, TEMP_CHANNEL, TEMP_FIELD, TEMP_SCALE);
        break;
    case 1:
        push_sample(raw, HUM_CHANNEL, HUM_FIELD, HUM_SCALE);
        break;
    default:
        break;
    }
}

/* Direct write whose payload includes an entry parameter, for testing
 * that unknown values are never invented. */
void sample_direct(int channel, int raw)
{
    db_write(channel, TEMP_FIELD, TEMP_SCALE, raw);
}

/* Loop with a concrete trip count. */
void sample_loop(int raw)
{
    int i;
    for (i = 0; i < 2; i++) {
        push_sample(raw, TEMP_CHANNEL + i, TEMP_FIELD, TEMP_SCALE);
    }
}

int main(void)
{
    sample_environment(0, 1);
    sample_environment_local(1, 2);
    sample_shared(1, 3);
    sample_struct(1, 4);
    sample_recursive(5);
    sample_unknown(6);
    sample_switch(0, 7);
    sample_loop(8);
    return 0;
}
