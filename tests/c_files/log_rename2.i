typedef unsigned int u32;
typedef unsigned char u8;

struct LinkX {
    int code;
    struct LinkX *next;
};

typedef struct {
    u32 ts;
    u8 level;
    struct LinkX *chain;
} Payload;
