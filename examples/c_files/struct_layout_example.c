typedef unsigned int u32;

struct Point {
    int x;
    int y;
};

struct Sample {
    u32 sequence;
    unsigned char flags;
    unsigned char channel:4;
    unsigned char range:2;
    unsigned char enabled:1;
    union {
        int analog;
        unsigned char digital[4];
    };
    struct Point calibration;
    long long timestamp;
} [[gnu::packed]];

struct AlignedRecord {
    short kind;
    int value [[gnu::aligned(16)]];
    unsigned char payload[6];
};

struct EventLog {
    unsigned int magic;
    unsigned int count;
    struct Sample first;
    struct Sample entries[];
};
